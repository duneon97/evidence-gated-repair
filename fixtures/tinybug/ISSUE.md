# Bug: `parse_duration("90m")` raises ValueError

`README.md` documents minutes-only (`90m`) and combined (`1h30m`) durations, but the parser
rejects them:

```python
>>> from src.duration import parse_duration
>>> parse_duration("2h")
7200
>>> parse_duration("90m")
ValueError: invalid duration: '90m'
>>> parse_duration("1h30m")
ValueError: invalid duration: '1h30m'
```

Expected behaviour, per the table in `README.md`:

- `parse_duration("90m") == 5400`
- `parse_duration("1h30m") == 5400`
- `parse_duration("2h") == 7200` still works
- suffixes should be case-insensitive (`2H` == `7200`)
- invalid input still raises `ValueError`, non-string still raises `TypeError`

Environment: Python 3.13, pytest, no third-party dependencies.
