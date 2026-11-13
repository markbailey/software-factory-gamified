"""Stand-in job for the walking skeleton.

Prints a few steps with progress markers so the game has something real to show.
Include the word "fail" in the spec to see a crate go to the scrap bin.
"""
import sys
import time

text = " ".join(sys.argv[1:]).strip() or "(empty spec)"
print(f"Received spec: {text}", flush=True)

steps = ["Reading spec", "Planning", "Working", "Checking", "Packing"]
for i, step in enumerate(steps, start=1):
    time.sleep(0.8)
    print(f"{step}...", flush=True)
    # Lines starting with PROGRESS are turned into job_progress events by the runner.
    print(f"PROGRESS {i / len(steps):.2f}", flush=True)

if "fail" in text.lower():
    print("Spec asked to fail; exiting with code 1", flush=True)
    sys.exit(1)

print("Done", flush=True)
