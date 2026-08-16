#src/finance.py

def simple_interest(principal, rate, time):
    return (principal * rate * time) / 100

def compound_interest(principal, rate, time, n):
    return principal * (1 + rate / (n * 100)) ** (n * time) - principal

