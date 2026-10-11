import io
import json
from pathlib import Path
import sys

import httpx
from PIL import Image
import pytest
from frankensurf import Runtime,WebPolicy
from frankensurf.identity import IdentityRegistry
from frankensurf import experimental
from frankensurf.runtime import parse_content,WebFailure

PROVIDERS=["camoufox","scrapling","scrapling_http"]
URL="https://www.gumtree.com.au/web/listing/lenses/12345"
GALLERY="https://images.gumtree.com.au/listing-lens.jpg"


def listing_html():
    product={"@type":"Product","url":URL,"sku":"12345","name":"Unknown lens collection",
        "description":"Lens identification needs a photograph, seller says autofocus shakes",
        "image":[GALLERY],"offers":{"url":URL,"price":250,"priceCurrency":"AUD","availability":"https://schema.org/InStock"}}
    recommendation={"@type":"Product","sku":"99999","name":"Another lens","image":["https://images.gumtree.com.au/other.jpg"]}
    return ('<link rel="canonical" href="'+URL+'"><title>Unknown lens collection</title><h1>Unknown lens collection</h1><p>Listing ID12345 - autofocus shakes</p>'
        + '<script type="application/ld+json">'+json.dumps(product)+'</script>'
        + '<script type="application/ld+json">'+json.dumps(recommendation)+'</script>'
        + '<img src="https://assets.test/logo.png"><img src="https://images.gumtree.com.au/recommendation.jpg">')


@pytest.mark.parametrize("provider",PROVIDERS)
async def test_registered_identity_denied_before_optional_worker_launch(tmp_path,monkeypatch,provider):
    path=tmp_path/'identities.json';registry=IdentityRegistry(path)
    registry.enroll_executor('desktop',endpoint='http://127.0.0.1:9331',user_data_dir=str(tmp_path/'profile'),profile_ref='personal-profile')
    registry.enroll_identity('personal',executor_id='desktop',domains=['www.gumtree.com.au'])
    async def unexpected(*args):raise AssertionError('Named identity must never reach anonymous worker')
    monkeypatch.setattr(experimental,'_packet',unexpected)
    async with Runtime(tmp_path/'state',identity_registry=path) as web:
        result=await web.read(URL,WebPolicy(provider=provider,identity='personal'))
    assert result['receipt']['failure']['code']=='IDENTITY_PROVIDER_DENIED'
    assert not result['receipt']['attempts'] and not result['receipt']['evidence']
    assert result['content']=='' and result['image_urls']==[]


async def test_empty_http200_javascript_shell_is_not_observed_or_cached(tmp_path,monkeypatch):
    async def shell(*args):return {'status':'ok','url':URL,'http_status':200,'content_type':'text/html','content':'<html><head><script>loadListingLater()</script></head><body></body></html>'}
    monkeypatch.setattr(experimental,'_packet',shell)
    async with Runtime(tmp_path) as web:result=await web.read(URL,WebPolicy(provider='scrapling'))
    assert result['receipt']['failure']['code']=='VISUAL_REQUIRED'
    assert result['receipt']['status']=='failed'
    assert result['receipt']['evidence']==[] and list((tmp_path/'cache').iterdir())==[]


@pytest.mark.parametrize('provider',['camoufox','scrapling'])
async def test_disabled_local_browser_does_not_spawn_worker(tmp_path,monkeypatch,provider):
    async def unexpected(*args):raise AssertionError('Local browser disabled')
    monkeypatch.setattr(experimental,'_packet',unexpected)
    async with Runtime(tmp_path) as web:result=await web.read(URL,WebPolicy(provider=provider,allow_local_browser=False))
    assert result['receipt']['failure']['code']=='POLICY_DENIED'


async def test_missing_provider_environment_is_typed_and_keeps_default_http_working(tmp_path,monkeypatch):
    monkeypatch.setenv('FRANKENSURF_PROVIDER_PYTHON',str(tmp_path/'missing-python'))
    async with Runtime(tmp_path,transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'ok':True}))) as web:
        missing=await web.read(URL,WebPolicy(provider='camoufox'))
        default=await web.read('https://public.test')
    assert missing['receipt']['failure']['code']=='PROVIDER_UNAVAILABLE'
    assert default['receipt']['method']=='http' and default['receipt']['status']=='observed'


async def test_worker_protocol_excludes_environment_secrets_and_scrubs_library_output(tmp_path,monkeypatch):
    # A real subprocess checks the allowlist, without requiring optional libraries.
    worker=tmp_path/'provider_worker.py'
    worker.write_text("import os,sys,json\nassert 'FRANKENSURF_FAKE_SECRET' not in os.environ\nr=json.load(sys.stdin)\nprint(json.dumps({'status':'ok','url':r['url'],'content':'public','content_type':'text/plain','http_status':200}))\n")
    monkeypatch.setattr(experimental,'__file__',str(tmp_path/'experimental.py'))
    monkeypatch.setenv('FRANKENSURF_PROVIDER_PYTHON',sys.executable)
    monkeypatch.setenv('FRANKENSURF_FAKE_SECRET','must-never-cross-worker-boundary')
    result=await experimental._packet(URL,WebPolicy(provider='camoufox'),'camoufox')
    assert result['content']=='public'


async def test_measured_public_route_uses_scrapling_readiness_without_changing_explicit_override(tmp_path,monkeypatch):
    monkeypatch.setattr(experimental,'installed',lambda _:True)
    calls=[]
    async def page(url,policy,provider):
        calls.append((provider,policy.wait_selector,policy.settle_ms))
        return {'status':'ok','url':url,'http_status':200,'content_type':'text/html','content':listing_html()}
    monkeypatch.setattr(experimental,'_packet',page)
    async with Runtime(tmp_path,transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'official':'override'}))) as web:
        routed=await web.read(URL)
        explicit=await web.read(URL,WebPolicy(provider='http'))
    assert calls==[('scrapling','h1',1500)]
    seed=routed['receipt']['routing']['catalog_seeds'][0]
    assert seed['basis']=='bundled provisional compatibility seed'
    assert seed['reliability'] is None
    assert routed['receipt']['method']=='scrapling' and explicit['receipt']['method']=='http'


async def test_catalog_seed_never_overrides_identity_or_disabled_browser(tmp_path,monkeypatch):
    monkeypatch.setattr(experimental,'installed',lambda _:True)
    transport=httpx.MockTransport(lambda _:httpx.Response(200,text='plain public evidence'))
    async with Runtime(tmp_path,transport=transport) as web:
        identity=await web.read(URL,WebPolicy(identity='personal'))
        disabled=await web.read(URL,policy_overrides={'allow_local_browser':False})
    assert identity['receipt']['failure']['code']=='IDENTITY_UNKNOWN'
    assert 'route_recipe' not in identity['receipt']
    assert all(attempt['provider']!='scrapling' for attempt in disabled['receipt']['attempts'])
    assert disabled['receipt']['routing']['operator_recipes']['skipped'][0]['reason']=='POLICY_DENIED'


async def test_nonexecutable_provider_configuration_returns_typed_failure(tmp_path,monkeypatch):
    file=tmp_path/'private-invalid-python';file.write_text('Not an interpreter');file.chmod(0o600)
    monkeypatch.setenv('FRANKENSURF_PROVIDER_PYTHON',str(file))
    async with Runtime(tmp_path/'state') as web:
        result=await web.read(URL,WebPolicy(provider='camoufox'))
    assert result['receipt']['failure']['code']=='PROVIDER_UNAVAILABLE'
    assert str(file) not in json.dumps(result)


def test_scrapling_safety_covers_explicit_and_implicit_security_defaults():
    from types import SimpleNamespace
    from frankensurf.provider_worker import _scrapling_safety
    session=SimpleNamespace(_browser_options={'args':['--no-sandbox','--disable-cookie-encryption',
        '--disable-features=HttpsUpgrades','--enable-features=NetworkServiceInProcess','--lang=en-AU'],
        'ignore_default_args':['--enable-automation']},_context_options={})
    feature_arg='--disable-features=HttpsUpgrades,ThirdPartyStoragePartitioning'
    _scrapling_safety(session,feature_arg)
    assert session._browser_options['args']==['--lang=en-AU']
    assert session._browser_options['chromium_sandbox'] is True
    assert feature_arg in session._browser_options['ignore_default_args']
    assert '--password-store=basic' in session._browser_options['ignore_default_args']
    assert '--use-mock-keychain' in session._browser_options['ignore_default_args']
    assert session._context_options['ignore_https_errors'] is False


@pytest.mark.parametrize('status,code',[(403,'BLOCKED'),(429,'RATE_LIMITED')])
async def test_scrapling_http_failure_precedes_readiness_timeout(monkeypatch,status,code):
    from types import SimpleNamespace
    from frankensurf import provider_worker
    class FakeSession:
        def __init__(self,**options):self.action=options['page_action']
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def fetch(self,url):
            class TimeoutError(Exception):pass
            async def wait_for(**options):raise TimeoutError()
            locator=SimpleNamespace(first=SimpleNamespace(wait_for=wait_for))
            page=SimpleNamespace(locator=lambda _:locator)
            await self.action(page)
            return SimpleNamespace(status=status,html_content='Not allowed',url=url)
    monkeypatch.setitem(sys.modules,'scrapling.fetchers',SimpleNamespace(AsyncStealthySession=FakeSession))
    monkeypatch.setattr(provider_worker,'_scrapling_safety',lambda _:None)
    result=await provider_worker.acquire({'provider':'scrapling','url':URL,'timeout_seconds':1,
        'max_bytes':50000,'settle_ms':0,'wait_selector':'h1','wait_state':'attached'})
    assert result=={'status':'failed','failure':code,'http_status':status,'url':URL}


async def test_worker_group_children_are_cleaned_after_successful_leader_exit(tmp_path,monkeypatch):
    import asyncio
    import os
    import signal
    pid_file=tmp_path/'owned-child.pid'
    worker=tmp_path/'provider_worker.py'
    worker.write_text("import subprocess,sys,json\n"+
        "r=json.load(sys.stdin)\n"+
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"+
        "open("+repr(str(pid_file))+",'w').write(str(child.pid))\n"+
        "print(json.dumps({'status':'ok','url':r['url'],'http_status':200,'content_type':'text/plain','content':'public'}))\n")
    monkeypatch.setattr(experimental,'__file__',str(tmp_path/'experimental.py'))
    monkeypatch.setenv('FRANKENSURF_PROVIDER_PYTHON',sys.executable)
    result=await experimental._packet(URL,WebPolicy(provider='scrapling'),'scrapling')
    assert result['content']=='public'
    pid=int(pid_file.read_text())
    try:
        await asyncio.sleep(.05)
        stat=Path('/proc')/str(pid)/'stat'
        assert not stat.exists() or stat.read_text().split(') ',1)[1].startswith('Z ')
    finally:
        try:os.kill(pid,signal.SIGKILL)
        except ProcessLookupError:pass


@pytest.mark.parametrize("url", ["https://www.ebay.com.au/sch/i.html?_nkw=camera", "https://www.ebay.com.au/itm/123456789", "https://www.carsales.com.au/cars/used/", "https://www.carsales.com.au/cars/details/used-car/OAG-AD-123/", "https://www.carsales.com.au/_api/gallery-core/OAG-AD-123/?itemId=0", "https://www.cashconverters.com.au/shop/cameras/digital-camera/123456789", "https://www.depop.com/products/seller-camera-123/", "https://www.depop.com/search/?q=camera", "https://www.depop.com/presentation/api/v1/search/products/?what=camera&limit=24&country=au&currency=AUD&from=in_country_search&include_like_count=true", "https://www.tradingpost.com.au/search-results/?q=camera", "https://www.tradingpost.com.au/electronics/camera/ad-abc123"])
def test_camoufox_compatibility_routes_are_typed_catalog_data(tmp_path,url,monkeypatch):
    from frankensurf.providers import ProviderManifest,ProviderRegistry
    from frankensurf.routes import RouteRecipeRegistry
    class Plugin:
        manifest=ProviderManifest('camoufox','legacy',rendering=True,requires_local_browser=True)
        def available(self,configured):return experimental.installed('camoufox')
        async def acquire(self,request,services):raise AssertionError
    registry=ProviderRegistry();registry.register(Plugin())
    monkeypatch.setattr(experimental,'installed',lambda _:True)
    routes=RouteRecipeRegistry(tmp_path/'missing.json')
    plan=routes.plan(url,'read',None,None,WebPolicy(),frozenset(),providers=registry)
    assert len(plan.seeds)==1
    assert plan.seeds[0].provider=='camoufox'
    assert plan.seeds[0].recipe.basis=='bundled provisional compatibility seed'
    assert plan.seeds[0].recipe.metadata()['reliability'] is None
    for policy in [WebPolicy(identity='personal'),WebPolicy(provider='camoufox'),WebPolicy(allow_local_browser=False)]:
        route=routes.plan(url,'read',None,None,policy,frozenset(),providers=registry)
        assert route.plans==() and route.seeds==()


def test_compatibility_route_does_not_cover_accounts_or_missing_install(tmp_path,monkeypatch):
    from frankensurf.providers import ProviderManifest,ProviderRegistry
    from frankensurf.routes import RouteRecipeRegistry
    class Plugin:
        manifest=ProviderManifest('camoufox','legacy',rendering=True,requires_local_browser=True)
        def available(self,configured):return experimental.installed('camoufox')
        async def acquire(self,request,services):raise AssertionError
    registry=ProviderRegistry();registry.register(Plugin())
    routes=RouteRecipeRegistry(tmp_path/'missing.json')
    monkeypatch.setattr(experimental,'installed',lambda _:True)
    unmatched=routes.plan('https://www.depop.com/settings/','read',None,None,WebPolicy(),frozenset(),providers=registry)
    assert unmatched.plans==() and unmatched.seeds==()
    monkeypatch.setattr(experimental,'installed',lambda _:False)
    plan=routes.plan('https://www.depop.com/products/seller-camera/','read',None,None,WebPolicy(),frozenset(),providers=registry)
    assert plan.plans==() and plan.seeds==() and plan.skipped[0]['reason']=='PROVIDER_UNAVAILABLE'


def test_gumtree_catalog_route_carries_readiness_without_core_site_logic(tmp_path,monkeypatch):
    from frankensurf.providers import ProviderManifest,ProviderRegistry
    from frankensurf.routes import RouteRecipeRegistry
    class Plugin:
        manifest=ProviderManifest('scrapling','legacy',rendering=True,requires_local_browser=True)
        def available(self,configured):return experimental.installed('scrapling')
        async def acquire(self,request,services):raise AssertionError
    registry=ProviderRegistry();registry.register(Plugin())
    monkeypatch.setattr(experimental,'installed',lambda _:True)
    routes=RouteRecipeRegistry(tmp_path/'missing.json')
    category='https://www.gumtree.com.au/s-lenses/sydney/c21108l3003435'
    selected=routes.plan(category,'read',None,None,WebPolicy(),frozenset(),providers=registry).seeds[0]
    detail=routes.plan(URL,'read',None,None,WebPolicy(),frozenset(),providers=registry).seeds[0]
    assert 'a.user-ad-row-new-design[href]' in selected.policy.wait_selector
    assert detail.policy.wait_selector=='h1'
    explicit=routes.plan(category,'read',None,None,WebPolicy(wait_selector='#caller'),
        frozenset({'wait_selector'}),providers=registry)
    assert explicit.plans==() and explicit.seeds==() and explicit.skipped[0]['reason']=='EXPLICIT_POLICY_CONFLICT'


@pytest.mark.parametrize("url", ["https://www.ebay.com.au/mye/myebay", "https://signin.ebay.com.au/", "https://www.ebay.com/itm/123456789", "https://www.ebay.com.au/itm/not-an-item"])
def test_ebay_compatibility_recipe_is_bounded(tmp_path,url,monkeypatch):
    from frankensurf.providers import ProviderManifest,ProviderRegistry
    from frankensurf.routes import RouteRecipeRegistry
    class Plugin:
        manifest=ProviderManifest('camoufox','legacy',rendering=True,requires_local_browser=True)
        def available(self,configured):return True
        async def acquire(self,request,services):raise AssertionError
    registry=ProviderRegistry();registry.register(Plugin())
    routes=RouteRecipeRegistry(tmp_path/'missing.json')
    plan=routes.plan(url,'read',None,None,WebPolicy(),frozenset(),providers=registry)
    assert plan.plans==() and plan.seeds==()


@pytest.mark.parametrize("stage,expected", [("category_navigation", " at category_navigation"),
    ("secret-token", ""), (["secret-token"], "")])
async def test_failure_stage_labels_are_safe_and_raw_details_are_withheld(tmp_path, monkeypatch, stage, expected):
    async def packet(*args):
        return {"status": "failed", "failure": "BLOCKED", "http_status": 403,
            "url": "https://www.bikesales.com.au/bikes/used/", "failure_stage": stage,
            "message": "secret-password"}
    monkeypatch.setattr(experimental, "_packet", packet)
    async with Runtime(tmp_path) as web:
        result = await web.read("https://www.bikesales.com.au/bikes/used/", WebPolicy(provider="camoufox"))
    assert result["receipt"]["failure"]["message"] == "Optional public provider acquisition failed" + expected
    assert "secret" not in json.dumps(result)


def test_worker_failure_labels_only_export_known_stages():
    from frankensurf.provider_worker import _failed
    assert _failed("BLOCKED", stage="category_navigation")["failure_stage"] == "category_navigation"
    assert "failure_stage" not in _failed("BLOCKED", stage="secret-token")

@pytest.mark.parametrize("retain,maximum,expected", [(True,4096,True),(False,4096,False),(True,1,False)])
async def test_failed_optional_response_is_private_evidence_not_success(tmp_path,monkeypatch,retain,maximum,expected):
    body="<html><title>Access denied</title><p>Public challenge page</p></html>"
    async def packet(*args):
        return {"status":"failed","failure":"BLOCKED","url":URL,"http_status":403,"content":body,"content_type":"text/html"}
    monkeypatch.setattr(experimental,"_packet",packet)
    async with Runtime(tmp_path) as web:
        result=await web.read(URL,WebPolicy(provider="scrapling",retain_public_failure_evidence=retain,max_bytes=maximum))
    assert result["receipt"]["status"]=="failed" and result["receipt"]["failure"]["code"]=="BLOCKED"
    assert result["content"]=="" and result["receipt"]["evidence"]==[]
    refs=result["receipt"]["attempts"][0].get("evidence",[])
    assert bool(refs)==expected and not list((tmp_path/"cache").iterdir())
    if expected:
        path=Path(refs[0]["path"])
        assert path.read_text()==body and path.stat().st_mode & 0o777 == 0o600


def test_worker_failure_content_respects_budget_policy_and_validated_url():
    from frankensurf.provider_worker import _failed,_failure_content
    request={"retain_public_failure_evidence":True,"max_bytes":100}
    result=_failure_content(_failed("BLOCKED",403,URL),"denied","text/html",request)
    assert result["status"]=="failed" and result["content"]=="denied"
    assert "content" not in _failure_content(_failed("BLOCKED",403,URL),"x"*101,"text/html",request)
    assert "content" not in _failure_content(_failed("BLOCKED",403,"https://user:pass@example.com"),"denied","text/html",request)
    assert "content" not in _failure_content(_failed("BLOCKED",403,URL),"denied","text/html",{**request,"retain_public_failure_evidence":False})


async def test_public_provider_cannot_omit_requested_content_readiness(tmp_path, monkeypatch):
    async def page(*args):
        return {"status": "ok", "url": URL, "http_status": 200,
            "content_type": "text/html", "content": listing_html()}
    monkeypatch.setattr(experimental, "_packet", page)
    async with Runtime(tmp_path) as web:
        result = await web.read(URL, WebPolicy(provider="scrapling",
            content_ready_selector="#ready", content_ready_timeout_seconds=2))
    assert result["receipt"]["failure"]["code"] == "PROVIDER_DOWN"
    assert result["receipt"]["status"] == "failed"


def test_scrapling_runs_unsandboxed_only_where_the_kernel_forbids_the_sandbox(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from frankensurf import provider_worker
    restricted, allowed = tmp_path / "restrict", tmp_path / "clone"
    monkeypatch.setattr(provider_worker, "_USERNS_SWITCHES", ((str(restricted), "1"), (str(allowed), "0")))

    def sandbox():
        session = SimpleNamespace(_browser_options={"args": []}, _context_options={})
        provider_worker._scrapling_safety(session, "--disable-features=x")
        return session._browser_options["chromium_sandbox"]
    assert sandbox() is True                      # neither switch exists: keep the sandbox
    restricted.write_text("0"); allowed.write_text("1")
    assert sandbox() is True                      # both allow it
    restricted.write_text("1")
    assert sandbox() is False                     # Ubuntu 23.10+ AppArmor restriction
    restricted.write_text("0"); allowed.write_text("0")
    assert sandbox() is False                     # older Debian switch
