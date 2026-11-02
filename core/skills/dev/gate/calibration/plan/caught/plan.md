| file | role | change | why |
| --- | --- | --- | --- |
| `models.py` | producer | rename field `total` to `amount_due` | the ask |
| `api.py` | consumer | read `amount_due`, keep the wire key `total` | it reads the field |

Verification: `pytest -q` after the rename.
