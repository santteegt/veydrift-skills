# jev engine scenarios

Each `*.json` file is one scenario, run by `tests/test_jev_engine.py`:

* offline, with a rule-based `FakeBackend` (`responder`), always;
* live, against the real TypeSafe API, only with `VEYDRIFT_JEV_LIVE_TESTS=1` and `TYPESAFE_API_KEY`
  (the `responder` key is ignored; a fallback to the ladder is reported, not failed, unless more
  than half of the scenarios fall back).

| key | meaning |
|---|---|
| `description` | one line, for humans |
| `snapshot` | a file name under `tests/fixtures/`, or `{"builder": "rich_one" \| "rich_two"}` (`tests/pool_fixtures.py`: a developed one- or two-planet account where every candidate family has something to say) |
| `snapshot_patch` | optional dict deep-merged into the snapshot's JSON (lists are replaced) |
| `planet_patches` | optional `{"<planet_id>": {...}}`, each deep-merged into that planet's JSON |
| `policy_overrides` | optional dict deep-merged into `pool_fixtures.make_policy()`'s JSON (everything actionable switched on) |
| `intent` | `engine.jev.intent` |
| `responder` | offline only: `{"fit_keywords": {"<text in what/group>": fit 0..1}, "default_fit", "urgency_by_group": {...}, "threat"}`, see `tests/jev_fakes.keyword_responder` |
| `acceptable` | list of acceptable picks; the chosen action matches if it matches any entry. An entry is `{"function": ..., "entity_name": ...}` (every key given must equal the action's) or `{"rule_prefix": "5:"}` |
| `backend_not_called` | optional; the scenario is decided by a veto or the deadline, so the backend must never be called |

Offline, a scenario must end with `engine == "jev"` (or a pre-empting rule) and an acceptable pick.
Live, an acceptable pick or a ladder fallback passes.
