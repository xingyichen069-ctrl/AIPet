"""Small, bounded direct HTTP reads and public Bing search; no paid fallback."""
import base64
import http.client
from html.parser import HTMLParser
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit


class WebError(RuntimeError):
    pass


def read(url, *, deadline=None, limit=2*1024*1024):
    deadline = min(deadline if deadline is not None else float('inf'), time.monotonic()+10)
    for _ in range(4):
        target = urlsplit(url)
        if (target.scheme not in ('http', 'https') or not target.hostname
                or target.username or target.password or len(url)>8192):
            raise WebError('网页地址格式不支持。')
        def left():
            remaining=deadline-time.monotonic()
            if remaining<=0:raise WebError('普通网页直连超时；未调用 Tavily。')
            return remaining
        cls=http.client.HTTPSConnection if target.scheme=='https' else http.client.HTTPConnection
        connection=cls(target.hostname,target.port,timeout=left())
        response=None
        try:
            connection.request('GET',(target.path or '/')+('?' + target.query if target.query else ''),
                               headers={'User-Agent':'Mozilla/5.0','Accept-Encoding':'identity'})
            sock=connection.sock
            sock.settimeout(left());response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                location=response.getheader('Location')
                if not location:raise WebError('网页跳转地址缺失。')
                url=urljoin(url,location);continue
            if response.status!=200:
                raise WebError(f'普通网页直连返回 HTTP {response.status}；未调用 Tavily。')
            raw=bytearray()
            while not response.isclosed():
                sock.settimeout(left())
                block=response.read1(min(32768,limit+1-len(raw)))
                if not block:break
                raw.extend(block)
                if len(raw)>limit:raise WebError('网页超过读取上限，已停止。')
            left()
            charset=response.headers.get_content_charset() or 'utf-8'
            try:return bytes(raw).decode(charset,errors='replace')
            except LookupError:return bytes(raw).decode('utf-8',errors='replace')
        except (OSError,http.client.HTTPException):
            raise WebError('普通网页直连失败或超时；未调用 Tavily。') from None
        finally:
            if response is not None:response.close()
            connection.close()
    raise WebError('网页跳转次数过多，已停止。')


def _link(url):
    parsed=urlsplit(url)
    if parsed.hostname and parsed.hostname.endswith('.bing.com') and parsed.path.startswith('/ck/'):
        value=parse_qs(parsed.query).get('u',[''])[0]
        if value.startswith('a1'):
            try:url=base64.urlsafe_b64decode(value[2:]+'='*(-len(value[2:])%4)).decode('utf-8')
            except (ValueError,UnicodeError):return ''
    return url if url.startswith(('https://','http://')) else ''


class Results(HTMLParser):
    def __init__(self):
        super().__init__();self.rows=[];self.depth=0;self.heading=False;self.paragraph=False

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='li':
            if self.depth:self.depth+=1
            elif 'b_algo' in attrs.get('class','').split():
                self.depth=1;self.row={'title':'','url':'','snippet':'','source':'Bing','date':''}
        if not self.depth:return
        if tag=='h2':self.heading=True
        if tag=='p':self.paragraph=True
        if tag=='a' and self.heading and not self.row['url']:
            self.row['url']=_link(attrs.get('href',''))

    def handle_data(self,text):
        if not self.depth:return
        if self.heading:self.row['title']+=text
        elif self.paragraph:self.row['snippet']+=text

    def handle_endtag(self,tag):
        if tag=='h2':self.heading=False
        if tag=='p':self.paragraph=False
        if tag=='li' and self.depth:
            self.depth-=1
            if not self.depth and self.row['title'].strip() and self.row['url']:
                self.row['title']=' '.join(self.row['title'].split())[:240]
                self.row['snippet']=' '.join(self.row['snippet'].split())[:900]
                self.rows.append(self.row)


def search(query,n,kind,*,deadline=None):
    params={'q':query,'setlang':'zh-hans','cc':'CN'}
    if kind=='news':params['filters']='ex1:"ez2"'  # Recent-week public web results.
    parser=Results();parser.feed(read('https://cn.bing.com/search?'+urlencode(params),deadline=deadline))
    if not parser.rows:
        raise WebError('普通搜索未得到可用结果，可能无匹配或遇到验证；未调用 Tavily。')
    return parser.rows[:n]


def text(url,*,deadline=None):
    html=read(url,deadline=deadline)
    html=re.sub(r'(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>',' ',html)
    class Content(HTMLParser):
        def __init__(self):super().__init__();self.parts=[]
        def handle_data(self,data):self.parts.append(data)
    parser=Content();parser.feed(html)
    return re.sub(r'\s+',' ',' '.join(parser.parts)).strip()[:100000]
