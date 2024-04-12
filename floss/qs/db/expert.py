import re
import pathlib
import pkgutil
from typing import Set, Dict, List, Tuple, Literal, Sequence
from dataclasses import dataclass

import msgspec

import floss.qs.db


class ExpertRule(msgspec.Struct):
    type: Literal["string", "substring", "regex"]
    value: str

    tag: str
    action: Literal["mute", "highlight", "hide"]
    note: str
    description: str

    authors: List[str]
    references: List[str]


@dataclass
class ExpertStringDatabase:
    string_rules: Dict[str, ExpertRule]
    substring_rules: List[ExpertRule]
    regex_rules: List[Tuple[ExpertRule, re.Pattern]]

    def __len__(self) -> int:
        return len(self.string_rules) + len(self.substring_rules) + len(self.regex_rules)

    def query(self, s: str) -> Set[str]:
        ret = set()

        if s in self.string_rules:
            ret.add(self.string_rules[s].tag)

        # note that this is O(m * n)
        # #strings * #rules
        for rule in self.substring_rules:
            if rule.value in s:
                ret.add(rule.tag)

        # note that this is O(m * n)
        # #strings * #rules
        for rule, regex in self.regex_rules:
            if regex.search(s):
                ret.add(rule.tag)

        return ret

    @classmethod
    def load_database(cls, buf: bytes) -> "ExpertStringDatabase":
        string_rules: Dict[str, ExpertRule] = {}
        substring_rules: List[ExpertRule] = []
        regex_rules: List[Tuple[ExpertRule, re.Pattern]] = []

        decoder = msgspec.json.Decoder(type=ExpertRule)

        for line in buf.split(b"\n"):
            if not line:
                continue

            rule = decoder.decode(line)
            match rule:
                case ExpertRule(type="string"):
                    # no duplicates today
                    string_rules[rule.value] = rule
                case ExpertRule(type="substring"):
                    substring_rules.append(rule)
                case ExpertRule(type="regex"):
                    # TODO: may have to cleanup the //gi from the regex
                    regex_rules.append((rule, re.compile(rule.value)))
                case _:
                    raise ValueError(f"unexpected rule type: {rule.type}")

        return cls(
            string_rules=string_rules,
            substring_rules=substring_rules,
            regex_rules=regex_rules,
        )

    @classmethod
    def from_file(cls, path: pathlib.Path) -> "ExpertStringDatabase":
        return cls.load_database(path.read_bytes())
    
    @classmethod
    def from_pkgutil(cls, package: str, path: str) -> "ExpertStringDatabase":
        return cls.load_database(pkgutil.get_data(package, path))


DEFAULT_PATHS = ( "data/expert/capa.jsonl",)


def get_default_databases() -> Sequence[ExpertStringDatabase]:
    # To use from_file
    # return [ExpertStringDatabase.from_file(pathlib.Path(floss.qs.db.__file__).parent / path) for path in DEFAULT_PATHS]

    return [ExpertStringDatabase.from_pkgutil("floss.qs.db", path) for path in DEFAULT_PATHS]
