import os
import sys

sys.path.insert(0, r'D:\\开发\\unified-rx-mcp\\bench')
sys.path.insert(0, r'D:\\开发\\unified-rx-mcp')
import tempfile

import registry

d = tempfile.mkdtemp()
p = os.path.join(d, 't.c')
open(p, 'w').write('int main() { return 0; }\n')
registry.call('bug_scan', {'path': p})
print('driver done')
