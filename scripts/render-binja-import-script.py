#!/usr/bin/env python3
# Copyright (C) 2021 Mandiant, Inc. All Rights Reserved.

"""
render-binja-import-script.py

Translate a floss result document into an Binary Ninja script
that marks up the current workspace.

Usage:

  $ floss suspicious.exe -j > floss-results.json
  $ python render-binja-import-script.py floss-results.json > apply_floss.py
  # now run `apply_floss.py` in Binary Ninja

Copyright (C) 2021 Mandiant, Inc. All Rights Reserved.
Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
You may obtain a copy of the License at: [package root]/LICENSE.txt
Unless required by applicable law or agreed to in writing, software distributed under the License
 is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and limitations under the License.
"""
import sys
import base64
import logging
import argparse

from floss.results import AddressType, ResultDocument

logger = logging.getLogger("floss.render-binja-import-script")


def render_binja_script(result_document: ResultDocument) -> str:
    """
    Create Binary Ninja script contents for BNDB file annotations.
    """
    main_commands = []
    for ds in result_document.strings.decoded_strings:
        if ds.string != "":
            b64 = base64.b64encode(ds.string.encode("utf-8")).decode("ascii")
            b64 = 'base64.b64decode("%s").decode("utf-8")' % (b64)
            if ds.address_type == AddressType.GLOBAL:
                main_commands.append('print("FLOSS: string \\"%%s\\" at global VA 0x%x" %% (%s))' % (ds.address, b64))
                main_commands.append('AppendComment(%d, "FLOSS: " + %s)' % (ds.address, b64))
            else:
                main_commands.append(
                    'print("FLOSS: string \\"%%s\\" decoded at VA 0x%x" %% (%s))' % (ds.decoded_at, b64)
                )
                main_commands.append('AppendComment(%d, "FLOSS: " + %s)' % (ds.decoded_at, b64))
    main_commands.append('print("Imported decoded strings from FLOSS")')

    for ss in result_document.strings.stack_strings:
        if ss.string != "":
            b64 = base64.b64encode(ss.string.encode("utf-8")).decode("ascii")
            b64 = 'base64.b64decode("%s").decode("utf-8")' % (b64)
            main_commands.append('AppendLvarComment(%d, "FLOSS stackstring: " + %s)' % (ss.function, b64))
    main_commands.append('print("Imported stackstrings from FLOSS")')

    for ts in result_document.strings.tight_strings:
        if ts.string != "":
            b64 = base64.b64encode(ts.string.encode("utf-8")).decode("ascii")
            b64 = 'base64.b64decode("%s").decode("utf-8")' % (b64)
            main_commands.append('AppendComment(%d, "FLOSS tightstring: " + %s)' % (ts.function, b64))
    main_commands.append('print("Imported tightstrings from FLOSS")')

    script_content = """import base64

import binaryninja as bn


def AppendComment(ea, s):

    s = s.encode('ascii')
    refAddrs = []
    for ref in bv.get_code_refs(ea):
        refAddrs.append(ref)

    for addr in refAddrs:
        fnc = bv.get_functions_containing(addr.address)
        fn = fnc[0]

        string = fn.get_comment_at(addr.address)

        if not string:
            string = s  # no existing comment
        else:
            if s in string:  # ignore duplicates
                return
            string = string + "\\n" + s

        fn.set_comment_at(addr.address, string)

def AppendLvarComment(fva, s):

    # stack var comments are not a thing in Binary Ninja so just add at top of function
    # and at location where it's used as an arg
    s = s.encode('ascii')
    fn = bv.get_function_at(fva)

    for addr in [fva, pc]:
        string = fn.get_comment_at(addr)

        if not string:
            string = s
        else:
            if s in string:  # ignore duplicates
                return
            string = string + "\\n" + s

        fn.set_comment(addr, string)

print("Annotating %d strings from FLOSS for %s")
%s

""" % (
        len(result_document.strings.decoded_strings) + len(result_document.strings.stack_strings) + len(result_document.strings.tight_strings),
        result_document.metadata.file_path,
        "\n".join(main_commands),
    )
    return script_content


def main():
    parser = argparse.ArgumentParser(description="Generate an Binary Ninja script to apply FLOSS results.")
    parser.add_argument("/path/to/report.json", help="path to JSON document from `floss --json`")

    logging_group = parser.add_argument_group("logging arguments")

    logging_group.add_argument("-d", "--debug", action="store_true", help="enable debugging output on STDERR")
    logging_group.add_argument(
        "-q", "--quiet", action="store_true", help="disable all status output except fatal errors"
    )

    args = parser.parse_args()
    args.report_path = getattr(args, "/path/to/report.json")

    if args.quiet:
        logging.basicConfig(level=logging.WARNING)
        logging.getLogger().setLevel(logging.WARNING)
    elif args.debug:
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
        logging.getLogger().setLevel(logging.INFO)

    result_document = ResultDocument.parse_file(args.report_path)

    print(render_binja_script(result_document))
    return 0


if __name__ == "__main__":
    sys.exit(main())
