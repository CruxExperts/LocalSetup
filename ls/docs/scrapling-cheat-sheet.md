---
status: ACTIVE
version: 5.6
owner_skill: ls-scrapling
---

# Scrapling Cheat Sheet

A comprehensive quick-reference for the Scrapling web scraping framework.

---

## Table of Contents

1. [Quick Reference](#quick-reference)
2. [CLI Commands](#cli-commands)
3. [Fetch Modes](#fetch-modes)
4. [Stealth & Anti-Bot](#stealth--anti-bot)
5. [Data Extraction](#data-extraction)
6. [Asset Extraction](#asset-extraction)
7. [Spider/Crawl System](#spidercrawl-system)
8. [MCP Server](#mcp-server)
9. [Best Practices](#best-practices)

---

## Quick Reference

### Which Fetcher to Use?

| Scenario | Fetcher | Speed | Stealth |
|----------|---------|-------|---------|
| Simple HTTP requests | `Fetcher` |  | ** |
| Dynamic/JS content | `DynamicFetcher` |  | *** |
| Protected sites, Cloudflare | `StealthyFetcher` |  | ***** |

### Import Pattern

```python
from scrapling.fetchers import (
    Fetcher,           # HTTP requests (curl_cffi)
    AsyncFetcher,      # Async HTTP
    DynamicFetcher,    # Browser automation
    StealthyFetcher,   # Anti-bot browser
    FetcherSession,    # HTTP session
    DynamicSession,    # Browser session
    StealthySession,   # Stealth session
    ProxyRotator       # Proxy rotation
)
```

---

## CLI Commands

### Extract Commands

```bash
# Basic GET -> markdown
scrapling extract get "https://example.com" output.md

# With CSS selector
scrapling extract get "https://example.com" articles.md --css-selector "article"

# With headers/proxy
scrapling extract get "https://example.com" output.md \
  -H "User-Agent: Mozilla/5.0" \
  --proxy "http://user:pass@host:8080"

# Stealth fetch for protected sites
scrapling extract stealthy-fetch "https://protected.com" output.md \
  --solve-cloudflare

# Dynamic fetch (JS rendering)
scrapling extract fetch "https://spa.example.com" output.md \
  --network-idle --wait-selector ".loaded"
```

### HTTP Methods

```bash
# POST with form data
scrapling extract post "https://api.example.com" response.md --data "key=value"

# POST with JSON
scrapling extract post "https://api.example.com" response.json --json '{"key":"value"}'

# PUT / DELETE
scrapling extract put "https://api.example.com" response.md --data "update=info"
scrapling extract delete "https://api.example.com" response.md
```

---

## Fetch Modes

### 1. Fetcher (HTTP Requests)

```python
from scrapling.fetchers import Fetcher

# Basic GET
page = Fetcher.get('https://example.com')

# With params
page = Fetcher.get('https://example.com/search', params={'q': 'query'})

# Browser impersonation
page = Fetcher.get('https://example.com', impersonate='chrome')
# Options: chrome, firefox, safari, edge, chrome_android, safari_ios, tor

# HTTP/3 support
page = Fetcher.get('https://example.com', http3=True)

# POST with form/JSON
page = Fetcher.post('https://example.com', data={'key': 'value'})
page = Fetcher.post('https://example.com', json={'key': 'value'})

# Proxy and retries
page = Fetcher.get('https://example.com',
    proxy='http://user:pass@host:8080',
    retries=3,
    retry_delay=2
)
```

### 2. DynamicFetcher (Browser)

```python
from scrapling.fetchers import DynamicFetcher

# Basic fetch
page = DynamicFetcher.fetch('https://example.com')

# Use real Chrome (if installed)
page = DynamicFetcher.fetch('https://example.com', real_chrome=True)

# Disable resources for speed
page = DynamicFetcher.fetch('https://example.com', disable_resources=True)

# Wait for network idle or selector
page = DynamicFetcher.fetch(
    'https://example.com',
    network_idle=True,
    wait_selector='.content',
    wait_selector_state='visible'  # attached, detached, visible, hidden
)

# Custom actions
from playwright.sync_api import Page

def scroll(page: Page):
    page.mouse.wheel(0, 500)

page = DynamicFetcher.fetch('https://example.com', page_action=scroll)
```

### 3. StealthyFetcher (Anti-Bot)

```python
from scrapling.fetchers import StealthyFetcher

# Basic stealth fetch
page = StealthyFetcher.fetch('https://protected-site.com')

# Solve Cloudflare
page = StealthyFetcher.fetch(
    'https://protected-site.com',
    solve_cloudflare=True,
    timeout=60000  # At least 60s for Cloudflare
)

# Full stealth config
page = StealthyFetcher.fetch(
    'https://protected-site.com',
    solve_cloudflare=True,
    block_webrtc=True,
    hide_canvas=True,
    real_chrome=True,
    google_search=True,
    proxy='http://user:pass@host:8080',
    locale='en-US',
    timezone_id='America/New_York'
)
```

### Sessions (Persistent Connections)

```python
from scrapling.fetchers import FetcherSession, StealthySession

# HTTP Session
with FetcherSession(impersonate='chrome', http3=True) as session:
    page1 = session.get('https://example.com/page1')
    page2 = session.post('https://example.com/form', data={'key': 'value'})

# Stealth Session with proxy rotation
from scrapling.fetchers import ProxyRotator

rotator = ProxyRotator([
    'http://proxy1:8080',
    'http://proxy2:8080',
    'http://user:pass@proxy3:8080'
])

with StealthySession(
    proxy_rotator=rotator,
    solve_cloudflare=True
) as session:
    page1 = session.fetch('https://protected1.com')
    page2 = session.fetch('https://protected2.com')
```

---

## Stealth & Anti-Bot

### Stealth Techniques

| Technique | Description | How to Enable |
|-----------|-------------|---------------|
| Browser Impersonation | Real TLS/browser fingerprint | `impersonate='chrome'` |
| User Agent Rotation | Real browser headers | Enabled by default |
| Google Referer | Set Google as referer | `google_search=True` (default) |
| WebRTC Block | Prevent IP leaks | `block_webrtc=True` |
| Canvas Noise | Prevent canvas fingerprinting | `hide_canvas=True` |
| Real Chrome | Use installed Chrome | `real_chrome=True` |
| Cloudflare Solver | Auto-solve challenges | `solve_cloudflare=True` |
| Proxy Rotation | Rotate through proxies | `proxy_rotator=ProxyRotator([...])` |

### Request Delays & Jitter

```python
import time
import random

def fetch_with_jitter(url, min_delay=1, max_delay=3):
    time.sleep(random.uniform(min_delay, max_delay))
    return Fetcher.get(url)

# Exponential backoff for retries
page = Fetcher.get(url, retries=3, retry_delay=lambda: random.uniform(1, 3))
```

### Proxy Rotation

```python
from scrapling.fetchers import ProxyRotator

# Basic rotation
rotator = ProxyRotator([
    'http://proxy1:8080',
    'http://proxy2:8080',
    'http://user:pass@proxy3:8080'
])

# Dictionary format for browser sessions
rotator = ProxyRotator([
    {'server': 'http://proxy1:8080', 'username': 'user', 'password': 'pass'},
    {'server': 'http://proxy2:8080'}
])

# Random strategy
import random

def random_strategy(proxies, current_index):
    idx = random.randint(0, len(proxies) - 1)
    return proxies[idx], idx

rotator = ProxyRotator(proxies, strategy=random_strategy)
```

---

## Data Extraction

### CSS Selectors

```python
# Select elements
items = page.css('.product')           # All matches
first = page.css('.product')[0]        # First match

# Extract text
text = page.css('h1::text').get()      # First text
texts = page.css('h1::text').getall()  # All texts

# Extract attributes
href = page.css('a::attr(href)').get()
src = page.css('img::attr(src)').getall()

# Nested selection
price = page.css('.product')[0].css('.price::text').get()

# Check class
if element.has_class('in-stock'):
    print("Available")
```

### XPath Selectors

```python
items = page.xpath('//*[@class="product"]')
text = page.xpath('//h1//text()').get()
href = page.xpath('//a/@href').get()
```

### Filter-Based Searching

```python
# By tag
page.find_all('div')

# By class
page.find_all('div', class_='quote')

# By attribute (contains, starts, ends)
page.find_all({'href*': '/author/'})   # Contains
page.find_all({'href^': 'https://'})   # Starts with
page.find_all({'href$': '.pdf'})       # Ends with

# By regex
import re
page.find_all(re.compile(r'GBP[\d\.]+'))

# Combine filters
page.find_all('div', {'class': 'quote'}, lambda e: 'world' in e.text)
```

### Text Matching

```python
# Exact text
element = page.find_by_text('Product Name')

# Partial text
results = page.find_by_text('Name', partial=True, first_match=False)

# Regex
price = page.find_by_regex(r'GBP[\d\.]+')
prices = page.find_by_regex(r'GBP[\d\.]+', first_match=False)
```

### Find Similar Elements

```python
# Find similar items in a grid
first = page.find_by_text('Add to Cart').find_ancestor(
    lambda e: e.has_class('product-card')
)
products = first.find_similar()

for p in products:
    print({
        'name': p.css('h3::text').get(),
        'price': p.css('.price::text').re_first(r'\d+\.\d{2}')
    })
```

### Regex Extraction

```python
# On elements
price = page.css('.price')[0].re_first(r'[\d\.]+')
prices = page.css('.price').re(r'[\d\.]+')

# On attributes
ids = page.css('a::attr(href)').re(r'catalogue/(.*)/index.html')
```

### Navigation

```python
# Ancestors/parents
container = element.find_ancestor(lambda e: e.has_class('product'))
parent = element.parent

# Children/descendants
children = element.children
text = element.get_all_text(strip=True)

# Generate selectors
css = element.generate_css_selector
xpath = element.generate_xpath_selector
```

---

## Asset Extraction

### Recommended Folder Structure

```
scraped_assets/
|-- images/           # .jpg, .png, .gif, .svg, .webp
|-- documents/        # .pdf, .doc, .docx, .txt, .csv
|-- videos/           # .mp4, .webm, .mov
|-- audio/            # .mp3, .wav, .ogg
|-- data/             # .json, .xml
`-- misc/             # Other types
```

### Download Functions

```python
import os
from urllib.parse import urlparse
from scrapling.fetchers import Fetcher

def download_asset(url, asset_type='misc', base_folder='scraped_assets'):
    """Download any asset with organized folder structure."""
    import mimetypes

    folder = os.path.join(base_folder, asset_type)
    os.makedirs(folder, exist_ok=True)

    # Fetch with appropriate timeout
    timeout = 300 if asset_type in ['videos', 'audio'] else 30
    page = Fetcher.get(url, timeout=timeout)

    # Determine filename
    parsed = urlparse(url)
    filename = os.path.basename(parsed.path)

    if not filename or '.' not in filename:
        content_type = page.headers.get('content-type', '').split(';')[0]
        ext = mimetypes.guess_extension(content_type) or '.bin'
        filename = f"asset{ext}"

    filepath = os.path.join(folder, filename)

    with open(filepath, 'wb') as f:
        f.write(page.body)

    return filepath

# Download all images from a page
page = Fetcher.get('https://example.com')
for img in page.css('img'):
    src = img.attrib.get('src')
    if src:
        full_url = page.urljoin(src)
        download_asset(full_url, 'images')

# Download all PDFs
for link in page.css('a[href$=".pdf"]'):
    url = page.urljoin(link.attrib['href'])
    download_asset(url, 'documents')
```

### Batch Asset Extraction

```python
def extract_all_assets(page, base_folder='scraped_assets'):
    """Extract and download all assets from a page."""
    downloads = []

    # Images
    for img in page.css('img'):
        src = img.attrib.get('src')
        if src:
            url = page.urljoin(src)
            downloads.append(download_asset(url, 'images', base_folder))

    # PDFs
    for link in page.css('a[href$=".pdf"]'):
        url = page.urljoin(link.attrib['href'])
        downloads.append(download_asset(url, 'documents', base_folder))

    return downloads
```

---

## Spider/Crawl System

### Basic Spider

```python
from scrapling.spiders import Spider, Response, Request

class MySpider(Spider):
    name = "my_spider"
    start_urls = ["https://example.com"]

    async def parse(self, response: Response):
        yield {
            'title': response.css('h1::text').get(''),
            'url': response.url
        }

        # Follow links
        for link in response.css('a::attr(href)').getall():
            yield response.follow(link, callback=self.parse_page)

    async def parse_page(self, response: Response):
        yield {
            'title': response.css('h1::text').get(''),
            'content': response.css('.content::text').get('')
        }

# Run
if __name__ == '__main__':
    result = MySpider().start()
    result.items.to_jsonl('output.jsonl')
```

### Spider with Sessions

```python
from scrapling.spiders import Spider, SessionManager
from scrapling.fetchers import FetcherSession, AsyncStealthySession

class MySpider(Spider):
    name = "my_spider"
    concurrent_requests = 8

    def configure_sessions(self, manager: SessionManager):
        manager.add('requests', FetcherSession(impersonate='chrome'))
        manager.add('browser', AsyncStealthySession(
            block_webrtc=True, solve_cloudflare=True
        ), lazy=True)

    async def parse(self, response: Response):
        yield {'title': response.css('h1::text').get('')}
```

### Block Detection & Retry

```python
class MySpider(Spider):
    max_blocked_retries = 3

    async def is_blocked(self, response: Response) -> bool:
        if response.status in {403, 429, 503}:
            return True
        body = response.body.decode('utf-8', errors='ignore')
        return 'access denied' in body.lower()

    async def retry_blocked_request(self, request, response):
        request.sid = 'stealth'
        return request
```

### Spider with Proxy Rotation

```python
from scrapling.fetchers import ProxyRotator

class MySpider(Spider):
    def configure_sessions(self, manager):
        rotator = ProxyRotator([
            'http://proxy1:8080',
            'http://proxy2:8080',
        ])
        manager.add('default', FetcherSession(proxy_rotator=rotator))
```

---

## MCP Server

### Claude Desktop Config

```json
{
  "mcpServers": {
    "ScraplingServer": {
      "command": "scrapling",
      "args": ["mcp"]
    }
  }
}
```

### MCP Tools

| Tool | Purpose |
|------|---------|
| `get` | Fast HTTP with browser fingerprinting |
| `bulk_get` | Async HTTP for multiple URLs |
| `fetch` | Dynamic content with browser |
| `stealthy_fetch` | Stealth browser (Cloudflare bypass) |

---

## Best Practices

### Rate Limiting

```python
import time
import random

def polite_fetch(url, min_delay=1, max_delay=3):
    time.sleep(random.uniform(min_delay, max_delay))
    return Fetcher.get(url)

# For spiders
class MySpider(Spider):
    download_delay = 1  # Base delay between requests
```

### Error Handling

```python
def safe_fetch(url, retries=3):
    for attempt in range(retries):
        try:
            page = Fetcher.get(url, timeout=30)
            if page.status == 200:
                return page
            elif page.status in [429, 503]:
                time.sleep(5 * (attempt + 1))
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
    return None
```

### Ethical Scraping Checklist

- [ ] Check `robots.txt` before scraping
- [ ] Implement reasonable rate limits
- [ ] Respect Terms of Service
- [ ] Don't overwhelm servers
- [ ] Cache results when possible
- [ ] Handle errors gracefully
- [ ] Don't scrape personal/private data

### Performance Tips

```python
# Disable resources for speed
page = DynamicFetcher.fetch(url, disable_resources=True)

# Use sessions for connection pooling
with FetcherSession() as session:
    for url in urls:
        page = session.get(url)

# Bulk operations
results = await AsyncFetcher.bulk_get(urls)
```

---

*Cheat sheet created for agent reference. Full docs: https://scrapling.readthedocs.io*
