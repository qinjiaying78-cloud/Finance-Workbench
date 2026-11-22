"""Publish only browser assets. Never package databases, attachments or local credentials."""
from pathlib import Path
import shutil
root=Path(__file__).resolve().parent.parent
dest=root/'dist'
dest.mkdir(exist_ok=True)
shutil.copy2(root/'index.html',dest/'index.html')
shutil.copytree(root/'assets',dest/'assets',dirs_exist_ok=True)
print('Static files written to dist/. Local API and Supabase integration remain separate services.')
