# file_transfer.py

import time

def simulate_file_transfer():
    """Simulates a file transfer by waiting for a specified duration."""
    transfer_time = 210  # Duration in seconds (3.5 minutes)
    time.sleep(transfer_time)
    return "File Transfer Complete"
