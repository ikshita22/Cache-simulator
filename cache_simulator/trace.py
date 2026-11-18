from collections.abc import Iterable, Iterator
from dataclasses import dataclass
import re


class TraceError(ValueError):
    pass


@dataclass(frozen=True)
class TraceAccess:
    core: int
    operation: str
    address: int


def parse_trace(
    lines: Iterable[str], cores: int = 4, source: str = "<trace>",
    *, core_id: int | None = None,
) -> Iterator[TraceAccess]:
    if type(cores) is not int or cores <= 0:
        raise ValueError("cores must be a positive integer")
    if core_id is not None and (
        type(core_id) is not int or not 0 <= core_id < cores
    ):
        raise ValueError(f"core_id must be between 0 and {cores - 1}")
    for number, line in enumerate(lines, 1):
        content = line.partition("#")[0].strip()
        if not content:
            continue
        fields = content.split()
        try:
            if core_id is not None:
                if len(fields) != 2:
                    raise ValueError("expected: <R|W> <address>")
                fields.insert(0, str(core_id))
            if len(fields) != 3:
                raise ValueError("expected: <core> <R|W> <address>")
            core_text, operation, address_text = fields
            if not re.fullmatch(r"[0-9]+", core_text):
                raise ValueError("core must be a decimal non-negative integer")
            core = int(core_text, 10)
            if not 0 <= core < cores:
                raise ValueError(f"core must be between 0 and {cores - 1}")
            operation = operation.upper()
            if operation not in ("R", "W"):
                raise ValueError("operation must be R or W")
            if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", address_text):
                raise ValueError("address must be decimal or 0x-prefixed hexadecimal")
            address = int(address_text, 16 if address_text.lower().startswith("0x") else 10)
            if address >= 1 << 64:
                raise ValueError("address must fit in 64 bits")
        except ValueError as error:
            raise TraceError(f"{source}:{number}: {error}") from error
        yield TraceAccess(core, operation, address)
