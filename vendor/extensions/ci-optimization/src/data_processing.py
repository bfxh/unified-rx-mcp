# data_processing.py

import time

def process_large_dataset():
    """Simulates processing a large dataset by performing computations."""
    start_time = time.time()
    duration = 200  # Duration in seconds (approximately 3.3 minutes)
    while time.time() - start_time < duration:
        # Simulate data processing by performing computations
        _ = [x ** 2 for x in range(10000)]
    return "Processing Complete"
