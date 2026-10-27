# tinybug

Tiny fixture repo used to exercise the evidence gate end to end. It contains one real bug.

## Duration format

`src/duration.py` must accept these forms and return whole seconds:

| Input | Meaning | Expected |
| --- | --- | --- |
| `2h` | two hours | `7200` |
| `90m` | ninety minutes | `5400` |
| `1h30m` | one hour thirty minutes | `5400` |
| `0h` | zero | `0` |

Anything else (unknown unit, empty string, garbage) raises `ValueError`. A non-string raises
`TypeError`. Suffixes are case-insensitive.
