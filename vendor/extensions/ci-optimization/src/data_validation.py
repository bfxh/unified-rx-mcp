def is_email_valid(email):
    return "@" in email and "." in email.split("@")[-1]

def is_phone_number_valid(phone):
    return phone.isdigit() and len(phone) == 10
