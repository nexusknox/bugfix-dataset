# bugfix-dataset

A pipeline that mines open-source repositories for real bugs, their fixes and
test evidence, verifies each one in Docker (tests fail before the fix and pass
after it), and exports a clean, licence-aware dataset for evaluating and
training AI coding models.

> Work in progress. Full README coming with the first release.

## License
Code: MIT, see [LICENSE](LICENSE). Each dataset record keeps the licence of the
repository it came from.
