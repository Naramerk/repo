tests/test_bench_live_runner.py: убраны доказуемо лишние Any — 14 → 3 вхождения
(grep -o | wc -l; было 14, стало 3). `ModuleType` вместо `Any` для динамически
загружаемого fake_llm; `**overrides: object` и `base: dict[str, object]` в
`_task`; `**overrides: object` и `overrides: dict[str, object]` в
`_config_dict`/parametrize-тесте; `_RecordingDriver.run` типирован точно по
протоколу `bench.drivers.Driver` (BenchTask, tuple[Path, ...], LiveRunnerConfig;
сверено с bench/drivers/__init__.py и единственным call-site
bench/live_runner.py:212, codegraph impact `_DRIVERS`);
в `calls` и `evidence` `Any` заменён на `object` (`dict[str, object]`).
Добавлены ids к обоим существующим parametrize
(правило «parametrize без ids запрещён»; имена узлов меняются, исходы — нет).

Оставленные маркеры (каждый с обоснованием):

- `Any` 3 вхождения (импорт + `-> dict[str, Any]` + `cfg: dict[str, Any]` в
  `_config_dict`): замена возвращаемого типа на `dict[str, object]` ломает
  `Path(cfg["data_root"])`, `Path(cfg["task_inputs_dir"])`,
  `Path(cfg_dict["tools_json"])` — `Path` требует `str | PathLike[str]`;
  честного точного типа без cast/type: ignore здесь нет.
- `getattr(entry.run, "detail", "")` в
  `test_live_runner_e2e_fake_llm_quality_compare_passes`: `entry.run` —
  `TimedRunOk | TimedRunRefused`, у `TimedRunOk` атрибута `detail` нет;
  сообщение assert вычисляется всегда, прямой доступ падал бы AttributeError
  на успешном пути — дефолт getattr достижим, код оставлен байт-в-байт.
- `# type: ignore[attr-defined]` на `server.cfg = ...` (ThreadingHTTPServer):
  ignore живой — без него mypy даёт attr-defined на присвоение необъявленного
  атрибута экземпляра; снятие потребовало бы подкласса-шима.

Счётчики до/после по файлу (вхождения, grep -o | wc -l): `Any` 14 → 3,
`getattr` 1 → 1, `type: ignore` 1 → 1, `cast(` 0 → 0, `audit-ok` 0 → 0.
Поведение, API, коды ошибок, логи и тексты сообщений не менялись.
