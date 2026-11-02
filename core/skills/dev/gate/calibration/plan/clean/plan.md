| file | role | change | why |
| --- | --- | --- | --- |
| `models.py` | producer | rename field `total` to `amount_due` | the ask |
| `report.py` | consumer | sum `amount_due` | it reads the field |
| `api.py` | consumer | read `amount_due`, keep the wire key `total` | it reads the field |

Verification: `pytest -q` after the rename.
