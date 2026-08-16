from urllib.parse import urlparse

def get_domain(url):
    parsed_url = urlparse(url)
    return parsed_url.netloc

def get_path(url):
    parsed_url = urlparse(url)
    return parsed_url.path
