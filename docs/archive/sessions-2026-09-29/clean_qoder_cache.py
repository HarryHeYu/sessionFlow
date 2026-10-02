"""Clean up qoder cache from C drive."""
import shutil
import os

BASE = r"C:\Users\He_Yu_Hao\.qoder"

# List of directories to remove
dirs_to_remove = [
    "tmp",
    "cache",
    "bin",  # This includes the large git-staging directory
]

total_size = 0
removed_count = 0

for d in dirs_to_remove:
    path = os.path.join(BASE, d)
    if os.path.exists(path):
        size_before = 0
        for root, dirs, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    size_before += os.path.getsize(fp)
                except:
                    pass

        print(f"Removing {d}... (approx. {size_before/1024/1024:.1f} MB)")
        shutil.rmtree(path, ignore_errors=True)
        removed_count += 1

print(f"\nDone! Removed {removed_count} directories from C:\\Users\\He_Yu_Hao\\.qoder")
print("No more qoder garbage on C drive!")
