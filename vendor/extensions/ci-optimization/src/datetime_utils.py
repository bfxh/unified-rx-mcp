from datetime import datetime

def get_current_time():
    return datetime.now()

def format_date(date, format_string):
    return date.strftime(format_string)