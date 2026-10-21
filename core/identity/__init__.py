"""Identity master (issue #85): what each security is and which sector and industry it belongs to.

``sources/`` holds one never-raising adapter per provider, ``cache.py`` keeps the last good
answer per source. The rules that turn source answers into ``security_master`` rows live here too (Part 2).
"""
