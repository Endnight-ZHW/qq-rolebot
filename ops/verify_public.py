"""Verify public TLS independently of any locally installed project CA."""
import argparse
import http.cookiejar
import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import certifi


def verify(require_onebot=False, base_url=None):
    fields={'Username':'admin','Password':os.environ.get('ADMIN_PASSWORD','')}
    base=(base_url or os.environ.get('PUBLIC_BASE_URL','')).rstrip('/')
    if not base or not fields['Password']:
        raise ValueError('Set PUBLIC_BASE_URL and ADMIN_PASSWORD for explicit public verification')
    parsed=urlsplit(base)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('Public verification requires an HTTPS origin')
    ctx=ssl.create_default_context(cafile=certifi.where())
    opener=urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def uncached(response):
        assert 'no-store' in response.headers.get('Cache-Control','').lower(), 'Response can be cached'
        assert int(response.headers.get('Age','0')) == 0, 'Edge returned an aged response'
        return response
    response=opener.open(base,timeout=20)
    uncached(response)
    result={'public_https':response.status,'tls_verified':True,'trust':'public-ca-bundle','html_bytes':len(response.read())}
    req=urllib.request.Request(base+'/api/v1/auth/login',data=json.dumps({'username':fields['Username'],'password':fields['Password']}).encode(),headers={'Content-Type':'application/json','Origin':base})
    response=uncached(opener.open(req,timeout=20))
    login=json.load(response)
    try:
        cookie=response.headers.get('Set-Cookie','').lower()
        assert all(flag in cookie for flag in ('secure','httponly','samesite=strict','max-age=43200')) and 'domain=' not in cookie, 'Unexpected session cookie policy'
        result['status']=json.load(uncached(opener.open(base+'/api/v1/status',timeout=20)))
        anonymous=urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        try:
            anonymous.open(base+'/api/v1/auth/me',timeout=10)
            raise AssertionError('Anonymous browser received an authenticated response')
        except urllib.error.HTTPError as exc:
            assert exc.code==401,exc.code
            uncached(exc)
        result['no_cache']=True
        result['anonymous_isolation']=True
        result['private_paths']={}
        for path in ['/onebot/v11/ws','/health/ready','/data/secrets.json','/internal/creation-media/nonexistent','/metrics']:
            try: code=opener.open(base+path,timeout=10).status
            except urllib.error.HTTPError as exc: code=exc.code
            result['private_paths'][path]=code
            assert code==404,(path,code)
        result['persona_names']=[p['name'] for p in json.load(opener.open(base+'/api/v1/personas',timeout=10))]
        if result['status']['onebot_connected']:
            result['qq_group_count']=len(json.load(opener.open(base+'/api/v1/groups/discover',timeout=20)))
        if require_onebot:
            assert result['status']['onebot_connected'],'NapCat is not connected'
    finally:
        req=urllib.request.Request(base+'/api/v1/auth/logout',data=b'{}',headers={'Content-Type':'application/json','Origin':base,'X-CSRF-Token':login['csrf']})
        opener.open(req,timeout=10).read()
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--require-onebot',action='store_true')
    parser.add_argument('--base-url',help='Explicit candidate HTTPS origin; does not edit saved credentials')
    args=parser.parse_args()
    print(json.dumps(verify(args.require_onebot,args.base_url),ensure_ascii=True))
