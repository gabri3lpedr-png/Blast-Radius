# PROBE_E decoding (probe branch only, NEVER pick as final entry)

Every FULL commit still runs all tests (0 new misses), packed so its cost is 38 s + 1.5 s x mark
of its FULL reason class. Main (08dedf9) scored 230.0 with 2 FULL commits at 36.817 s each, so
board score = 230.0 - 73.634 + 76 + 1.5 x (mark_a + mark_b). Closest two rows are 1.5 s apart.

| board score | reason of FULL commit 1 | reason of FULL commit 2 |
|---|---|---|
| 232.4 | new file under tests/ | new file under tests/ |
| 233.9 | new file under tests/ | new file under sqlglot/ |
| 235.4 | new file under sqlglot/ | new file under sqlglot/ |
| 239.9 | new file under tests/ | other new/deleted/rename/binary |
| 241.4 | new file under sqlglot/ | other new/deleted/rename/binary |
| 247.4 | other new/deleted/rename/binary | other new/deleted/rename/binary |
| 250.4 | new file under tests/ | structural/unrecognised |
| 251.9 | new file under sqlglot/ | structural/unrecognised |
| 257.9 | other new/deleted/rename/binary | structural/unrecognised |
| 268.4 | structural/unrecognised | structural/unrecognised |
| 269.9 | new file under tests/ | syntax error/patch problem |
| 271.4 | new file under sqlglot/ | syntax error/patch problem |
| 272.9 | new file under tests/ | name chain (alias/wide/deep) |
| 274.4 | new file under sqlglot/ | name chain (alias/wide/deep) |
| 277.4 | other new/deleted/rename/binary | syntax error/patch problem |
| 280.4 | other new/deleted/rename/binary | name chain (alias/wide/deep) |
| 284.9 | new file under tests/ | import-time (class_users/dunder/no plain names) |
| 286.4 | new file under sqlglot/ | import-time (class_users/dunder/no plain names) |
| 287.9 | structural/unrecognised | syntax error/patch problem |
| 290.9 | structural/unrecognised | name chain (alias/wide/deep) |
| 292.4 | other new/deleted/rename/binary | import-time (class_users/dunder/no plain names) |
| 293.9 | new file under tests/ | lookup found nothing |
| 295.4 | new file under sqlglot/ | lookup found nothing |
| 298.4 | new file under tests/ | other |
| 299.9 | new file under sqlglot/ | other |
| 301.4 | other new/deleted/rename/binary | lookup found nothing |
| 302.9 | structural/unrecognised | import-time (class_users/dunder/no plain names) |
| 305.9 | other new/deleted/rename/binary | other |
| 307.4 | syntax error/patch problem | syntax error/patch problem |
| 310.4 | syntax error/patch problem | name chain (alias/wide/deep) |
| 311.9 | structural/unrecognised | lookup found nothing |
| 313.4 | name chain (alias/wide/deep) | name chain (alias/wide/deep) |
| 316.4 | structural/unrecognised | other |
| 322.4 | syntax error/patch problem | import-time (class_users/dunder/no plain names) |
| 325.4 | name chain (alias/wide/deep) | import-time (class_users/dunder/no plain names) |
| 331.4 | syntax error/patch problem | lookup found nothing |
| 334.4 | name chain (alias/wide/deep) | lookup found nothing |
| 335.9 | syntax error/patch problem | other |
| 337.4 | import-time (class_users/dunder/no plain names) | import-time (class_users/dunder/no plain names) |
| 338.9 | name chain (alias/wide/deep) | other |
| 346.4 | import-time (class_users/dunder/no plain names) | lookup found nothing |
| 350.9 | import-time (class_users/dunder/no plain names) | other |
| 355.4 | lookup found nothing | lookup found nothing |
| 359.9 | lookup found nothing | other |
| 364.4 | other | other |

A score that matches no row: a different number of FULL commits, or the practice tests.json
differs. Misses should stay at 0 (every FULL commit still runs everything).
