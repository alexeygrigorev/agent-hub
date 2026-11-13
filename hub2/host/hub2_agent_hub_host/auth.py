"""Device authorization and refresh rotation; credential values never reach output."""
import sys
import time
import webbrowser
from urllib.parse import urlsplit,parse_qs
from .client import ApiError,Client,save_config


def store_tokens(config,path,server,reply):
    for key in ('access_token','refresh_token'):
        if not isinstance(reply.get(key),str) or not 32<=len(reply[key])<=4096:
            raise ValueError('invalid_token_response')
    if reply.get('token_type')!='Bearer' or type(reply.get('expires_in')) is not int or not 1<=reply['expires_in']<=86400:
        raise ValueError('invalid_token_response')
    config.update(server=server,token=reply['access_token'],refresh_token=reply['refresh_token'],expires_at=time.time()+reply['expires_in'])
    config.pop('refresh_pending',None)
    save_config(path,config)
    return Client(server,config['token'])


def refresh(config,path,server):
    if config.get('refresh_pending') or not config.get('refresh_token'):
        raise ValueError('interactive_login_required')
    # A lost refresh response must never replay a consumed token and revoke its family.
    config['refresh_pending']=True
    save_config(path,config)
    reply=Client(server).request('POST','/api/token/refresh',{'refresh_token':config['refresh_token']})
    return store_tokens(config,path,server,reply)


def authenticate(client,args,config):
    action=args.action
    if action=='status':return {'server':client.base_url,'configured':bool(client.token),'refresh_pending':bool(config.get('refresh_pending'))}
    if action=='refresh':
        refresh(config,args.config,client.base_url)
        return {'refreshed':True}
    if action=='logout':
        try:
            try:reply=client.request('POST','/api/token/revoke',{})
            except ApiError as error:
                if error.status!=401 or not config.get('refresh_token') or client.token!=config.get('token'):raise
                client=refresh(config,args.config,client.base_url)
                reply=client.request('POST','/api/token/revoke',{})
            if reply.get('revoked') is not True:raise ValueError('remote_revocation_unverified')
        finally:
            for key in ('token','refresh_token','expires_at','refresh_pending'):config.pop(key,None)
            save_config(args.config,config)
        return {'logged_out':True,'revoked':True}
    public=Client(client.base_url)
    if config.get('machine_id'):raise ValueError('operator_login_requires_separate_config')
    device=public.request('POST','/api/device/code',{'client_id':'hub-cli'})
    uri=device.get('verification_uri_complete','')
    target,origin=urlsplit(uri),urlsplit(client.base_url)
    user=device.get('user_code')
    if not isinstance(user,str) or not 6<=len(user)<=32 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in user):raise ValueError('invalid_device_response')
    if (target.scheme,target.netloc,target.path)!=(origin.scheme,origin.netloc,'/device') or target.fragment or parse_qs(target.query)!={'user_code':[user]} or any(ord(c)<32 for c in uri):
        raise ValueError('untrusted_device_approval_url')
    if not isinstance(device.get('device_code'),str) or not 32<=len(device['device_code'])<=4096:
        raise ValueError('invalid_device_response')
    expiry,interval=device.get('expires_in'),device.get('interval')
    if type(expiry) is not int or not 1<=expiry<=600 or type(interval) is not int or not 5<=interval<=60:
        raise ValueError('invalid_device_response')
    print('Approve Hub CLI access: '+uri,file=sys.stderr,flush=True)
    if not args.no_browser:webbrowser.open(uri)
    deadline=time.monotonic()+expiry
    while time.monotonic()+interval<deadline:
        time.sleep(interval)
        try:
            reply=public.request('POST','/api/device/token',{'device_code':device['device_code']})
            store_tokens(config,args.config,client.base_url,reply)
            return {'authenticated':True,'server':client.base_url}
        except ApiError as error:
            if error.status==428 and error.code=='authorization_pending':continue
            if error.status==429 and error.code in {'slow_down','rate_limited'}:
                interval=min(60,interval+5)
                continue
            raise
    raise ValueError('device_authorization_expired')
