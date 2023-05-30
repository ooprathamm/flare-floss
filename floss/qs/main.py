import abc
import io
import re
import sys
import json
import mmap
import time
import logging
import pathlib
import argparse
import itertools
import contextlib
from typing import Any, Set, Dict, Union, Literal, Iterable, Optional, Sequence
from dataclasses import dataclass, field

import pefile
import colorama
import lancelot
import intervaltree
import rich.traceback
from rich.text import Text
from rich.style import Style
from rich.console import Console

import floss.main
import floss.qs.db.oss
import floss.qs.db.winapi
from floss.qs.db.gp import StringHashDatabase, StringGlobalPrevalenceDatabase
from floss.qs.db.oss import OpenSourceStringDatabase
from floss.qs.db.expert import ExpertStringDatabase
from floss.qs.db.winapi import WindowsApiStringDatabase

MIN_STR_LEN = 6

logger = logging.getLogger("quantumstrand")


@contextlib.contextmanager
def timing(msg: str):
    t0 = time.time()
    yield
    t1 = time.time()
    logger.debug("perf: %s: %0.2fs", msg, t1 - t0)


ASCII_BYTE = r" !\"#\$%&\'\(\)\*\+,-\./0123456789:;<=>\?@ABCDEFGHIJKLMNOPQRSTUVWXYZ\[\]\^_`abcdefghijklmnopqrstuvwxyz\{\|\}\\\~\t".encode(
    "ascii"
)
ASCII_RE_6 = re.compile(b"([%s]{%d,})" % (ASCII_BYTE, 6))
UNICODE_RE_6 = re.compile(b"((?:[%s]\x00){%d,})" % (ASCII_BYTE, 6))


@dataclass
class Range:
    offset: int
    length: int

    @property
    def end(self) -> int:
        return self.offset + self.length

    def __contains__(self, other: Union[int, "Range"]) -> bool:
        if isinstance(other, int):
            return self.offset <= other < self.end
        elif isinstance(other, Range):
            return (other.offset in self) and (other.end in self)
        else:
            raise TypeError(f"unsupported type: {type(other)}")


@dataclass
class ExtractedString:
    string: str
    range: Range
    encoding: Literal["ascii", "unicode"]


Tag = str


@dataclass
class TaggedString:
    string: ExtractedString
    tags: Set[Tag]
    structure: str = ""


def extract_ascii_strings(buf: bytes, n: int = MIN_STR_LEN) -> Iterable[ExtractedString]:
    """Extract ASCII strings from the given binary data."""

    if not buf:
        return

    r = None
    if n == MIN_STR_LEN:
        r = ASCII_RE_6
    else:
        reg = b"([%s]{%d,})" % (ASCII_BYTE, n)
        r = re.compile(reg)
    for match in r.finditer(buf):
        offset = match.start()
        length = match.end() - match.start()
        yield ExtractedString(match.group().decode("ascii"), Range(offset, length), "ascii")


def extract_unicode_strings(buf: bytes, n: int = MIN_STR_LEN) -> Iterable[ExtractedString]:
    """Extract naive UTF-16 strings from the given binary data."""
    if not buf:
        return

    if n == MIN_STR_LEN:
        r = UNICODE_RE_6
    else:
        reg = b"((?:[%s]\x00){%d,})" % (ASCII_BYTE, n)
        r = re.compile(reg)
    for match in r.finditer(buf):
        offset = match.start()
        length = match.end() - match.start()
        try:
            yield ExtractedString(match.group().decode("utf-16"), Range(offset, length), "unicode")
        except UnicodeDecodeError:
            pass


MUTED_STYLE = Style(color="gray50")
DEFAULT_STYLE = Style()
HIGHLIGHT_STYLE = Style(color="yellow")


def Span(text: str, style: Style = DEFAULT_STYLE) -> Text:
    """convenience function for single-line, styled text region"""
    return Text(text, style=style, no_wrap=True, overflow="ellipsis", end="")


PADDING_WIDTH = 2
OFFSET_WIDTH = 8
STRUCTURE_WIDTH = 16


def render_string_padding():
    return Span(" " * PADDING_WIDTH)


TagRules = Dict[Tag, Literal["mute"] | Literal["highlight"] | Literal["default"] | Literal["hide"]]


def should_hide_string(s: TaggedString, tag_rules: TagRules) -> bool:
    return any(map(lambda tag: tag_rules.get(tag) == "hide", s.tags))


def compute_string_style(s: TaggedString, tag_rules: TagRules) -> Optional[Style]:
    """compute the style for a string based on its tags

    returns: Style, or None if the string should be hidden.
    """
    styles = set(tag_rules.get(tag, "mute") for tag in s.tags)

    # precedence:
    #
    #  1. highlight
    #  2. hide
    #  3. mute
    #  4. default
    if "highlight" in styles:
        return HIGHLIGHT_STYLE
    elif "hide" in styles:
        return None
    elif "mute" in styles:
        return MUTED_STYLE
    else:
        return DEFAULT_STYLE


def render_string_string(s: TaggedString, tag_rules: TagRules) -> Text:
    string_style = compute_string_style(s, tag_rules)
    if string_style is None:
        raise ValueError("string should be hidden")

    # render like json, but strip the leading/trailing quote marks.
    # this means that whitespace characters like \t and \n will be rendered as such,
    # which ensures that the rendered string will be a single line.
    rendered_string = json.dumps(s.string.string)[1:-1]
    return Span(rendered_string, style=string_style)


def render_string_tags(s: TaggedString, tag_rules: TagRules):
    ret = Text()

    tags = s.tags
    if len(tags) != 1 and "#common" in tags:
        # don't show #common if there are other tags,
        # because the other tags will be more specific (like library names).
        tags.remove("#common")

    for i, tag in enumerate(sorted(tags)):
        tag_style = DEFAULT_STYLE
        rule = tag_rules.get(tag, "mute")
        if rule == "highlight":
            tag_style = HIGHLIGHT_STYLE
        elif rule == "mute":
            tag_style = MUTED_STYLE
        elif rule == "default":
            tag_style = DEFAULT_STYLE
        else:
            raise ValueError(f"unknown tag rule: {rule}")

        ret.append_text(Span(tag, style=tag_style))
        if i < len(s.tags) - 1:
            ret.append_text(Span(" "))

    return ret


def render_string_offset(s: TaggedString):
    # render the 000 prefix of the 8-digit offset in muted gray
    # and the non-zero suffix as blue.
    offset_chars = f"{s.string.range.offset:08x}"
    unpadded = offset_chars.lstrip("0")
    padding_width = len(offset_chars) - len(unpadded)

    offset = Span("")
    offset.append_text(Span("0" * padding_width, style=MUTED_STYLE))
    offset.append_text(Span(unpadded, style=Style(color="blue")))

    return offset


def render_string_structure(s: TaggedString):
    ret = Text()

    if s.structure:
        structure = Span("/" + s.structure, style=MUTED_STYLE)
        structure.align("left", STRUCTURE_WIDTH)
        ret.append(structure)
    else:
        ret.append_text(Span(" " * STRUCTURE_WIDTH))

    return ret


def render_string(width: int, s: TaggedString, tag_rules: TagRules) -> Text:
    #
    #  | stringstringstring              #tag #tag #tag  00000001 |
    #  | stringstring                              #tag  0000004A |
    #  | string                                    #tag  00000050 |
    #  | stringstringstringstringstringst...  #tag #tag  0000005E |
    #    ^                                  ^ ^        ^ ^
    #    |                                  | |        | offset
    #    |                                  | |        padding
    #    |                                  | tags
    #    |                                  padding
    #    string
    #
    #    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^  ^^^^^^^^^^^^^^^^^^^^^^
    #    left column                       right column
    #
    # fields are basically laid out from right to left,
    # which means that the metadata may cause a string to be clipped.
    #
    # field sizes:
    #   structure: 8
    #   padding: 2
    #   offset: 8
    #   padding: 2
    #   tags: variable, or 0
    #   padding: 2
    #   string: variable

    left = render_string_string(s, tag_rules)

    right = Span("")
    right.append_text(render_string_padding())
    right.append_text(render_string_tags(s, tag_rules))
    right.append_text(render_string_padding())
    right.append_text(render_string_offset(s))
    right.append_text(render_string_structure(s))

    # this alignment clips the string if it's too long,
    # leaving an ellipsis at the end when it would collide with a tag/offset.
    # this is bad for showing all data verbatim,
    # but is good for the common case of triage analysis.
    left.align("left", width - len(right))

    line = Text()
    line.append_text(left)
    line.append_text(right)

    return line


def get_reloc_range(pe: pefile.PE) -> Optional[Range]:
    directory_index = pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_BASERELOC"]

    if pe.OPTIONAL_HEADER is None or pe.OPTIONAL_HEADER.DATA_DIRECTORY is None:
        return None

    try:
        dir_entry = pe.OPTIONAL_HEADER.DATA_DIRECTORY[directory_index]
    except IndexError:
        return None

    rva = dir_entry.VirtualAddress
    rsize = dir_entry.Size

    return Range(pe.get_offset_from_rva(rva), rsize)


def check_is_reloc(reloc: Optional[Range], string: ExtractedString):
    if not reloc:
        return ()

    if string.range in reloc:
        return ("#reloc",)
    else:
        return ()


def query_global_prevalence_database(db: StringGlobalPrevalenceDatabase, string: str):
    if db.query(string):
        return ("#common",)

    return ()


def query_global_prevalence_hash_database(db: StringHashDatabase, string: str):
    if string in db:
        return ("#common",)

    return ()


def query_library_string_databases(dbs: Sequence[OpenSourceStringDatabase], string: str) -> Sequence[Tag]:
    tags = set()
    for db in dbs:
        meta = db.metadata_by_string.get(string)
        if not meta:
            continue

        tags.add(f"#{meta.library_name}")

    return tuple(tags)


def query_expert_string_database(db: ExpertStringDatabase, string: str) -> Sequence[Tag]:
    return tuple(db.query(string))


def query_winapi_name_database(db: WindowsApiStringDatabase, string: str) -> Sequence[Tag]:
    if string.lower() in db.dll_names:
        return ("#winapi",)

    if string in db.api_names:
        return ("#winapi",)

    return ()


@dataclass
class Layout(abc.ABC):
    range: Range

    # human readable name
    name: str

    # ordered by address
    # non-overlapping
    # may not cover the entire range (non-contiguous)
    children: Sequence["Layout"]

    parent: Optional["Layout"]

    @property
    def predecessor(self) -> Optional["Layout"]:
        """ traverse to the prior sibling """
        if self.parent is None:
            return None

        index = self.parent.children.index(self)
        if index == 0:
            return None

        return self.parent.children[index - 1]

    @property
    def successor(self) -> Optional["Layout"]:
        """ traverse to the next sibling """
        if self.parent is None:
            return None

        index = self.parent.children.index(self)
        if index == len(self.parent.children) - 1:
            return None

        return self.parent.children[index + 1]



@dataclass
class PELayout(Layout):
    pass


@dataclass
class SectionLayout(Layout):
    section: pefile.SectionStructure


@dataclass
class SegmentLayout(Layout):
    pass


@dataclass
class ResourceLayout(Layout):
    buf: bytes


def compute_pe_layout(pe: pefile.PE, pe_offset: int) -> Layout:
    layout = PELayout(
        range=Range(0, len(pe.__data__)),
        name="pe",
        children=[],
        parent=None,
    )

    for section in sorted(pe.sections, key=lambda s: s.PointerToRawData):
        if section.SizeOfRawData == 0:
            continue

        try:
            name = section.Name.partition(b"\x00")[0].decode("utf-8")
        except UnicodeDecodeError:
            name = "(invalid)"

        layout.children.append(SectionLayout(
            range=Range(pe_offset + section.get_PointerToRawData_adj(), section.SizeOfRawData),
            name=name,
            children=[],
            parent=layout,
            section=section
        ))

    # segment that contains all data until the first section
    layout.children.insert(0, SegmentLayout(
        range=Range(pe_offset + 0, layout.children[0].range.offset - (pe_offset + 0)), 
        name="PE header",
        children=[],
        parent=layout,
    ))

    # segment that contains all data after the last section
    # aka. "overlay"
    last_section: Layout = layout.children[-1]
    if pe.__data__ is not None:
        buf = pe.__data__
        if last_section.range.end < pe_offset + len(buf):
            layout.children.append(SegmentLayout(
                range=Range(pe_offset + last_section.range.end, len(buf) - last_section.range.end), 
                name="PE overlay",
                children=[],
                parent=layout,
            ))

    # add segments for any gaps between sections.
    # note that we append new items to the end of the list and then resort,
    # to avoid mutating the list while we're iterating over it.
    for i in range(1, len(layout.children)):
        prior: Layout = layout.children[i - 1]
        region: Layout = layout.children[i]

        if prior.range.end != region.range.offset:
            layout.children.append(SegmentLayout(
                range=Range(pe_offset + prior.range.end, region.range.offset - prior.range.end),
                name="gap",
                children=[],
                parent=layout,
            ))

    layout.children.sort(key=lambda s: s.range.offset)

    if hasattr(pe, "DIRECTORY_ENTRY_RESOURCE"):

        def walk_resources(dir_data: pefile.ResourceDirData, path: Sequence[str] = ()):
            resources = []
            for entry in dir_data.entries:
                if entry.name:
                    name = str(entry.name)
                else:
                    name = str(entry.id)

                epath = path + (name,)

                if hasattr(entry, "directory"):
                    resources.extend(walk_resources(entry.directory, epath))

                else:
                    try:
                        buf = pe.get_data(entry.data.struct.OffsetToData, entry.data.struct.Size)
                    except pefile.PEFormatError:
                        continue
                    else:
                        logger.debug("resource: %s, size: 0x%x", "/".join(epath), len(buf))
                        pass

                    resources.append(ResourceLayout(
                        range=Range(pe_offset + entry.data.struct.OffsetToData, entry.data.struct.Size),
                        name="rsrc: " + "/".join(epath),
                        children=[],
                        # fixed up later when inserted into container
                        parent=None,
                        buf=buf,
                    ))

            return resources

        resources = walk_resources(pe.DIRECTORY_ENTRY_RESOURCE)
        resources.sort(key=lambda r: r.range.offset)

        for resource in resources:
            if resource.buf.startswith(b"MZ"):
                child_pe = pefile.PE(data=resource.buf)
                resource.children.append(
                    compute_pe_layout(child_pe, resource.range.offset)
                )

        for resource in resources:
            container = next(filter(lambda l: l.range.offset <= resource.range.offset < l.range.end, layout.children))
            resource.parent = container
            container.children.append(resource)
            container.children.sort(key=lambda l: l.range.offset)

    return layout


@dataclass
class Structure:
    range: Range
    name: str


def compute_file_structures(pe: pefile.PE) -> Sequence[Structure]:
    structures = []

    for section in sorted(pe.sections, key=lambda s: s.PointerToRawData):
        structures.append(Structure(Range(section.get_file_offset(), section.sizeof()), "section header"))

    if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
        for dll in pe.DIRECTORY_ENTRY_IMPORT:
            try:
                dll_name = dll.dll.decode("ascii")
            except UnicodeDecodeError:
                continue
            structures.append(Structure(Range(pe.get_offset_from_rva(dll.struct.Name), len(dll_name)), "import table"))

            for entry in dll.imports:
                if entry.name is None:
                    continue

                if entry.name_offset is None:
                    continue

                try:
                    symbol_name = entry.name.decode("ascii")
                except UnicodeDecodeError:
                    continue

                structures.append(Structure(Range(entry.name_offset, len(symbol_name)), "import table"))

    return structures


def main():
    # set environment variable NO_COLOR=1 to disable color output.
    # set environment variable FORCE_COLOR=1 to force color output, such as when piping to a pager.
    parser = argparse.ArgumentParser(description="Extract human readable strings from binary data, quantum-style.")
    parser.add_argument("path", help="file or path to analyze")
    logging_group = parser.add_argument_group("logging arguments")
    logging_group.add_argument("-d", "--debug", action="store_true", help="enable debugging output on STDERR")
    logging_group.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="disable all status output except fatal errors",
    )
    args = parser.parse_args()

    floss.main.set_log_config(args.debug, args.quiet)
    rich.traceback.install()
    if isinstance(sys.stdout, io.TextIOWrapper) or hasattr(sys.stdout, "reconfigure"):
        # from sys.stdout type hint:
        #
        # TextIO is used instead of more specific types for the standard streams,
        # since they are often monkeypatched at runtime. At startup, the objects
        # are initialized to instances of TextIOWrapper.
        #
        # To use methods from TextIOWrapper, use an isinstance check to ensure that
        # the streams have not been overridden:
        #
        # if isinstance(sys.stdout, io.TextIOWrapper):
        #    sys.stdout.reconfigure(...)
        sys.stdout.reconfigure(encoding="utf-8")
    colorama.just_fix_windows_console()

    path = pathlib.Path(args.path)
    if not path.exists():
        logging.error("%s does not exist", path)
        return 1

    format: Literal["binary", "pe"] = "binary"
    with path.open("rb") as f:
        WHOLE_FILE = 0

        if hasattr(mmap, "MAP_PRIVATE"):
            # unix
            kwargs = {"flags": mmap.MAP_PRIVATE, "prot": mmap.PROT_READ}
        else:
            # windows
            kwargs = {"access": mmap.ACCESS_READ}

        with mmap.mmap(f.fileno(), length=WHOLE_FILE, **kwargs) as mm:
            # treat the mmap as a readable bytearray
            buf: bytearray = mm  # type: ignore

            strings = list(
                sorted(
                    itertools.chain(extract_ascii_strings(buf), extract_unicode_strings(buf)),
                    key=lambda s: s.range.offset,
                )
            )

            pe: Optional[pefile.PE] = None
            layout: Layout = SegmentLayout(range=Range(0, len(buf)), name="binary", children=[], parent=None)
            structures: Sequence[Structure] = []
            try:
                pe = pefile.PE(data=buf)
            except pefile.PEFormatError:
                # this is ok, we'll just process the file as raw binary
                logger.debug("not a PE file")
            else:
                format = "pe"
                layout = compute_pe_layout(pe, 0)
                structures = compute_file_structures(pe)

            ws: Optional[lancelot.Workspace] = None
            # contains the file offsets of bytes that are part of recognized instructions.
            code_offsets = set()
            if format == "pe":
                # lancelot only accepts bytes, not mmap
                # TODO: fix bug during load of pma05-01
                # fixed in https://github.com/williballenthin/lancelot/issues/185
                with timing("lancelot: load workspace"):
                    try:
                        ws = lancelot.from_bytes(bytes(buf))
                    except ValueError as e:
                        logger.warning("lancelot failed to load workspace: %s", e)

                with timing("lancelot: find code"):
                    if ws is not None and pe is not None:
                        base_address = ws.base_address
                        for function in ws.get_functions():
                            cfg = ws.build_cfg(function)
                            for bb in cfg.basic_blocks.values():
                                # VA -> RVA -> file offset
                                offset = pe.get_offset_from_rva(bb.address - base_address)
                                for addr in range(offset, offset + bb.length):
                                    code_offsets.add(addr)

            reloc_range = get_reloc_range(pe) if pe else None

            # pe is not valid outside of this block
            # because the underlying mmap is closed.
            del pe

    data_path = pathlib.Path(floss.qs.db.oss.__file__).parent / "data"

    winapi_database = floss.qs.db.winapi.WindowsApiStringDatabase.from_dir(data_path / "winapi")

    capa_expert_database = ExpertStringDatabase.from_file(data_path / "expert" / "capa.jsonl")

    oss_database_filenames = (
        "brotli.jsonl.gz",
        "bzip2.jsonl.gz",
        "cryptopp.jsonl.gz",
        "curl.jsonl.gz",
        "detours.jsonl.gz",
        "jemalloc.jsonl.gz",
        "jsoncpp.jsonl.gz",
        "kcp.jsonl.gz",
        "liblzma.jsonl.gz",
        "libsodium.jsonl.gz",
        "libpcap.jsonl.gz",
        "mbedtls.jsonl.gz",
        "openssl.jsonl.gz",
        "sqlite3.jsonl.gz",
        "tomcrypt.jsonl.gz",
        "wolfssl.jsonl.gz",
        "zlib.jsonl.gz",
    )

    library_databases = [
        OpenSourceStringDatabase.from_file(data_path / "oss" / filename) for filename in oss_database_filenames
    ]

    library_databases.append(OpenSourceStringDatabase.from_file(data_path / "crt" / "msvc_v143.jsonl.gz"))

    tagged_strings = list(map(lambda s: TaggedString(s, set()), strings))

    gp_path = data_path / "gp"
    global_prevalence_database = StringGlobalPrevalenceDatabase.from_file(gp_path / "gp.jsonl.gz")
    global_prevalence_database.update(StringGlobalPrevalenceDatabase.from_file(gp_path / "cwindb-native.jsonl.gz"))
    global_prevalence_database.update(StringGlobalPrevalenceDatabase.from_file(gp_path / "cwindb-dotnet.jsonl.gz"))
    global_prevalence_hash_database_xaa = StringHashDatabase.from_file(gp_path / "xaa-hashes.bin")
    global_prevalence_hash_database_yaa = StringHashDatabase.from_file(gp_path / "yaa-hashes.bin")

    def check_is_code(code_offsets, string: ExtractedString):
        for addr in range(string.range.offset, string.range.end):
            if addr in code_offsets:
                return ("#code",)

        return ()

    structures_by_range = intervaltree.IntervalTree()
    for interval in structures:
        structures_by_range.addi(interval.range.offset, interval.range.end, interval)

    for string in tagged_strings:
        key = string.string.string

        string.tags.update(check_is_code(code_offsets, string.string))
        string.tags.update(check_is_reloc(reloc_range, string.string))

        string.tags.update(query_global_prevalence_database(global_prevalence_database, key))
        string.tags.update(query_global_prevalence_hash_database(global_prevalence_hash_database_xaa, key))
        string.tags.update(query_global_prevalence_hash_database(global_prevalence_hash_database_yaa, key))
        string.tags.update(query_library_string_databases(library_databases, key))
        string.tags.update(query_expert_string_database(capa_expert_database, key))
        string.tags.update(query_winapi_name_database(winapi_database, key))

        r = string.string.range
        overlapping_structures = list(sorted(structures_by_range.overlap(r.offset, r.end), key=lambda i: i.begin))
        for interval in overlapping_structures:
            # interval: intervaltree.Interval
            #
            # need intervaltree type annotations
            # Interval has the property .data, which is of type Structure,
            # due to how we initialized the map.
            structure: Structure = interval.data  # type: ignore
            string.structure = structure.name
            break

    # open source libraries should have at least 5 strings,
    # or don't show their tag, since the couple hits are probably false positives.
    #
    # hack: assume the libname is embedded in the filename.
    # otherwise, we don't have an easy way to recover the library tag names.
    for filename in oss_database_filenames:
        libname = filename.partition(".")[0]
        tagname = f"#{libname}"

        count = 0
        for string in tagged_strings:
            if tagname in string.tags:
                count += 1

        if 0 < count < 5:
            # I picked 5 as a reasonable threshold.
            # we could research what a better value is.
            #
            # also note that large binaries with many strings have
            # a higher chance of false positives, even with this threshold.
            # this is still a useful filter, though.
            for string in tagged_strings:
                if tagname in string.tags:
                    string.tags.remove(tagname)

    tag_rules: TagRules = {
        "#code": "hide",
        "#reloc": "hide",
        "#common": "mute",
        "#zlib": "mute",
        "#bzip2": "mute",
        "#sqlite3": "mute",
        "#winapi": "mute",
        "#wolfssl": "mute",
        "#capa": "highlight",
    }

    tagged_strings = list(filter(lambda s: not should_hide_string(s, tag_rules), tagged_strings))

    def render_layout_strings(layout: Layout, strings: Sequence[TaggedString], width: int, depth: int = 0):

        if isinstance(layout, PELayout) and not layout.predecessor and not layout.successor:
            for child in layout.children:
                render_layout_strings(child, strings, width, depth)
            return

        strings_in_range = list(filter(lambda s: layout.range.offset <= s.string.range.offset < layout.range.end, strings))

        if not strings_in_range:
            # don't render sections with no strings
            return

        BORDER_STYLE = Style(color="grey50")

        header = Span(layout.name, style=BORDER_STYLE)
        header.pad(1)
        header.align("center", width=console.width, character="━")

        # box is muted color
        # name of section is blue
        name_offset = header.plain.index(" ") + 1
        header.stylize(Style(color="blue"), name_offset, name_offset + len(layout.name))

        for _ in range(depth):
            header.remove_suffix("━")

        header.remove_suffix("━")

        header_shape = "┫"
        if not layout.predecessor:
            header_shape = "┓"
        else:
            # annoying to duplicate this logic here.
            # refactor is probably needed.
            predecessor = layout.predecessor
            strings_in_prev_range = list(filter(lambda s: predecessor.range.offset <= s.string.range.offset < predecessor.range.end, strings))

            if not strings_in_prev_range:
                header_shape = "┓"
            else:
                header_shape = "┫"
        header.append_text(Span(header_shape, style=BORDER_STYLE))

        for _ in range(depth):
            header.append_text(Span("┃", style=BORDER_STYLE))

        console.print(header)

        if layout.children:
            for i, child in enumerate(layout.children):
                # this is not efficient, rescanning the entire list, but optimize later.
                # this most clearly describes what we're trying to do.
                #
                # an optimization: since we know the strings are sorted,
                # we can keep track of the last string we saw, and quickly slice
                # the list of candidate strings, rather than scanning from the start again.
                if i == 0:
                    strings_before_child = list(filter(lambda s: layout.range.offset <= s.string.range.offset < child.range.offset, strings_in_range))
                else:
                    last_child = layout.children[i - 1]
                    strings_before_child = list(filter(lambda s: last_child.range.end < s.string.range.offset < child.range.offset, strings_in_range))

                if strings_before_child:
                    for string in strings_before_child[:4]:
                        line = render_string(console.width, string, tag_rules)
                        # TODO: this truncates the structure column
                        line = line[:-depth - 1]
                        for _ in range(depth + 1):
                            line.append_text(Span("┃", style=BORDER_STYLE))
                        console.print(line)

                render_layout_strings(child, strings_in_range, width, depth + 1)

            strings_after_children = list(filter(lambda s: child.range.end < s.string.range.offset < layout.range.end, strings_in_range))
            if strings_after_children:
                for string in strings_after_children[:4]:
                    line = render_string(console.width, string, tag_rules)
                    # TODO: this truncates the structure column
                    line = line[:-depth - 1]
                    for _ in range(depth + 1):
                        line.append_text(Span("┃", style=BORDER_STYLE))
                    console.print(line)

        else:
            for string in strings_in_range[:4]:
                line = render_string(console.width, string, tag_rules)
                # TODO: this truncates the structure column
                line = line[:-depth - 1]
                for _ in range(depth + 1):
                    line.append_text(Span("┃", style=BORDER_STYLE))
                console.print(line)

        should_print_footer = False
        if not layout.successor:
            should_print_footer = True
        else:
            # annoying to duplicate this logic here.
            # refactor is probably needed.
            successor = layout.successor
            strings_in_next_range = list(filter(lambda s: successor.range.offset <= s.string.range.offset < successor.range.end, strings))

            if not strings_in_next_range:
                should_print_footer = True

        if should_print_footer:
            footer = Span("", style=BORDER_STYLE)
            footer.align("center", width=console.width, character="━")

            for _ in range(depth):
                footer.remove_suffix("━")

            footer.remove_suffix("━")
            footer.append_text(Span("┛", style=BORDER_STYLE))

            for _ in range(depth):
                footer.append_text(Span("┃", style=BORDER_STYLE))

            console.print(footer)

    console = Console()
    render_layout_strings(layout, tagged_strings, 80)

    return 0


    if segments:
        for i, segment in enumerate(segments):
            header = Span(key, style=Style(color="blue"))
            header.pad(1)
            header.align("center", width=console.width, character="━")
            console.print(header)

            for string in strings_in_segment:
                if not should_hide_string(string, tag_rules):
                    s = render_string(console.width, string, tag_rules)
                    console.print(s)

    else:
        for string in tagged_strings:
            if not should_hide_string(string, tag_rules):
                s = render_string(console.width, string, tag_rules)
                console.print(s)

    return 0


if __name__ == "__main__":
    sys.exit(main())
