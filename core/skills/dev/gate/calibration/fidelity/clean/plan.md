| file | role | change | why |
| --- | --- | --- | --- |
| `sync.py` | producer | add `--dry-run`; `run(dry_run=True)` prints each batch instead of sending | the ask |
| `README.md` | doc | document `--dry-run` | users find flags there |

Verification: run `sync --dry-run` on a sample and confirm nothing is sent.
