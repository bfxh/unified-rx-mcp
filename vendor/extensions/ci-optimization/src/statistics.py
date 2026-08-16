# src/statistics.py

def mean(data):
    return sum(data) / len(data) if data else 0

def median(data):
    data_sorted = sorted(data)
    n = len(data_sorted)
    mid = n // 2
    if n % 2 == 0:
        return (data_sorted[mid - 1] + data_sorted[mid]) / 2
    else:
        return data_sorted[mid]
 # type: ignore