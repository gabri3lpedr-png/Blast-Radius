# Map check (tools/check_map.py on map v3)

Naive selector: union of every test the map links to the change; a patched .py that no longer compiles, module-level import-only code, or a new/unread file -> FALLBACK (run all). Grades the MAP, not the real selector.

## sample/ (30 commits)
```
commit kind      #sel   sel s #fail  MISS  note
2haz   code        86    30.8     0  no    
2jrq   code        10    24.8     8  no    
2qn5   code         6     0.5     0  no    
2vdj   fixture      1     0.1     1  no    
3utz   code        37    35.0    37  no    
4d63   code       273    10.2     0  no    
535z   code       171    14.5     1  no    
5y72   code         3     0.5     0  no    
66ks   code        51    22.8     0  no    
6vvp   fixture      1     0.0     1  no    
6ze8   code         1     0.0     1  no    
8y7j   code         0     0.0     0  no    
986c   multi     1620   103.0     1  no    FALLBACK sqlglot/dialects/singlestore.py:1 module-level; sqlglot/dialects/singlestore.py:2 module-level
9u5k   comment    104    12.1     0  no    
ary6   code        10     2.4    10  no    
bap8   comment   1620   103.0     0  no    FALLBACK sqlglot/transforms.py:911 module-level
bj7g   code         2     0.2     1  no    
bvw2   multi     1620   103.0     1  no    FALLBACK sqlglot/dialects/materialize.py:2 module-level; sqlglot/dialects/materialize.py:3 module-level
egys   fixture      1     0.6     1  no    
f4bn   fixture      1     0.0     1  no    
f4xa   comment     32     8.8     0  no    
fryt   code         3     0.2     0  no    
gbav   code        32     0.2     0  no    
gbhk   fixture      1     0.1     1  no    
j6uz   code         2     2.4     2  no    
ktz3   code         3     0.1     0  no    
mthh   fixture      1     1.0     1  no    
nwnx   fixture      1     0.1     1  no    
rszg   multi     1620   103.0     3  no    FALLBACK sqlglot/optimizer/scope.py:10 module-level; sqlglot/optimizer/scope.py:9 module-level
st8f   fixture      1     0.0     1  no    

commits: 30   commits with a MISS: 0   fallbacks (run all): 4   total selected seconds: 579.5
```

## mutants_out/ (labelled mutants, dev-only, not in git)
```
commits: 189   commits with a MISS: 0   fallbacks (run all): 50   total selected seconds: 6718.8
```
