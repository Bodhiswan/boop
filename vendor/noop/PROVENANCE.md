Source: https://github.com/ryanbr/noop
Commit: 7f396e98ed9d259df08e3a0a58cfac05fc70615c
File copied unchanged: Tools/linux-capture/whoop_frame.py
Required Notice: Copyright 2026 NoopApp
License: PolyForm Noncommercial License 1.0.0 (see LICENSE)

# Additional derived resources

`breath_protocols.json` retains the 22 protocol names, timing stages, educational text
and cautions in `Packages/StrandAnalytics/Sources/StrandAnalytics/BreathProtocolCatalog.swift`
at the same pinned commit. BOOP's Python analytics cite the source engine in docstrings
and tests. The upstream license and notices apply to these derivatives.

## Bundled parser test fixtures

The seven files in `test-fixtures/` are unchanged copies from
`Packages/StrandImport/Tests/StrandImportTests/Resources/` at the same pinned
commit above, under the included PolyForm Noncommercial license and required
notice. They make BOOP's importer tests independent of a reference checkout.

These are the upstream's small, fixed parser test samples, not BOOP recordings
or user exports. Review found no account identifiers or personal names in the
CSV/XML samples, and no author metadata or external links in the two XLSX
containers. They exercise example BOOP-compatible rows, Apple Health records and lifting
programs. Only files used by BOOP tests are included; the reference repository
and its Git metadata are excluded.

| File | SHA-256 |
| --- | --- |
| `journal_entries.csv` | `7a31e562e817e413416045e947a1d08cbcb1c5ddeeb5922868f021e66b324839` |
| `physiological_cycles.csv` | `a60c27d986202634e2b5eeb852c4861bd6ea214842734cf5c7ca7929bfe24c5b` |
| `sleeps.csv` | `97328a38ee2c1b26da528ceedfd6c2058c5bf45d68781e3defe67f08520db491` |
| `workouts.csv` | `e0a11a1c81b6867c9d54916b782d1424a87af17cc44ae8eda23b89b1fd5b2495` |
| `sample_health_data.xml` | `e968e7c07c54b08bdfda519be60839f273982f02d27746df8121605939431677` |
| `lift_program_filled.xlsx` | `51a110713406a4d4f35521456b6ed132ea2993d0e4baa79be188fd91d0b3da4e` |
| `lift_program_tabs_reordered.xlsx` | `f52c12062e9bfbee2e8bb8096aa271849956eebe4b4461f50b5912888c83fbf2` |
