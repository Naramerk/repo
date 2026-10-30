tests/test_pr20_proposed_check.py: убраны мёртвые и обходящие типизацию маркеры там, где тип доказуем; поведение и API не изменены.

Счётчики по файлу, вхождения (grep -o | wc -l), до/после: Any 0/0; getattr 10/4; type: ignore 7/3; "cast(" 6/6 — все шесть это вызовы upcast(), реального typing.cast в файле нет ни до, ни после. Проверено также counts.sh (строки): по изменённому файлу роста нет.

Убран type: ignore[arg-type] у вызова approve_check в _approve_human: аннотация параметра source заменена на точный ApprovalSource (Literal["hitl", "api", "reviewer"]); все 9 мест вызова хелпера передают литерал "api" либо используют дефолт; хелпер файл-локальный, внешних импортов нет.

Убраны три мёртвых type: ignore в class _Boom (no-untyped-def x2, assignment); мёртвость подтверждена mypy --warn-unused-ignores на файле.

Убраны 4 getattr с гарантированно объявленными атрибутами: _hitl_requests (объявлен в StudyRuntime.__init__, core/execution/machine.py:130) в двух местах, _hitl_decisions (machine.py:129), payload_ref у значений rt.hitl в трёх местах — карта типизирована dict[str, HitlRequest], поле payload_ref обязательное, все писатели карты создают только HitlRequest (experimental/runtime.py:54, 62, 80).

Оставленные маркеры. type: ignore[call-arg] на 1006 и 1163, type: ignore[arg-type] на 1165: намеренные негативные пробы (неверный kwarg / недопустимый source="human") под pytest.raises(TypeError/ValueError); без ignore mypy отверг бы сам тест. getattr(rt, "hitl", None) и getattr(row, "payload_ref", None) на 2109-2110: атрибут hitl отсутствует на статическом типе machine.StudyRuntime (есть только у experimental-рунтайма), и тот же comprehension сознательно поддерживает dict-строки через isinstance(row, dict), поэтому дефолт достижим по замыслу. getattr(rt, "_proposed_check_reviews", None) на 2460 и getattr(rt, "_hitl_decision_lock_owners", {}) на 2793: атрибуты создаются лениво продакшн-кодом (_reviews_map в core/plan/proposed_check.py:67-72, _decision_lock_owners в proposed_check.py:434-439), в __init__ их нет, дефолт достижим.

Проверки: ruff check — чисто; mypy по файлу — 153 ошибки до и после, набор идентичен (tests вне mypy-scope CI, новых ошибок нет); pytest tests/test_pr20_proposed_check.py — 66 passed. Цепочки вызовов проверены CodeGraph: callers open_runtime_experimental (8 мест создания rt; все три правимых теста используют его), impact approve_check (28 затронутых символов), query по конструированию HitlRequest; код читался через lean-ctx read (signatures/lines); для правки файла использован нативный Read/Edit.
