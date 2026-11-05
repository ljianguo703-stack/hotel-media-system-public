"""Shared native quality policy for fresh installs and explicit IN17 configuration migration."""
def allow_library_qualities(profile):
    def configure(item):
        if item.get('items'):
            for child in item['items']:configure(child)
            item['allowed']=any(child.get('allowed',False) for child in item['items'])
        else:
            quality=item.get('quality') or {}
            name=quality.get('name','').lower()
            item['allowed']=quality.get('resolution',0)<=1080 and not any(word in name for word in ('remux','raw-hd','br-disk'))
    for item in profile.get('items',[]):configure(item)
    return profile
