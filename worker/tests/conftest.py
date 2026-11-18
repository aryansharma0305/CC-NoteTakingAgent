import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))  # worker/ -> `import app`
sys.path.insert(0, str(HERE.parents[2]))  # repo root -> `import notion_publisher`
