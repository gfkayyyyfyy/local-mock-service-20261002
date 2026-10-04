"""入口：python -m mock_server --rules rules.json [--port 8765] [--check-rules]"""

import sys

from . import main

if __name__ == "__main__":
    sys.exit(main())
