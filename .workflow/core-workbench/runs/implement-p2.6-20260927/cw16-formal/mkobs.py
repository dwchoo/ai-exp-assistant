import json,sys
# usage: mkobs.py <execution.json> <items.json> <observed.json>
ex=json.load(open(sys.argv[1])); items=json.load(open(sys.argv[2]))
import os
assert not os.path.exists(sys.argv[3]), "observed exists"
ex["items"]=items
json.dump(ex,open(sys.argv[3],"w"),ensure_ascii=False,sort_keys=True)
print("wrote",sys.argv[3],len(items))
