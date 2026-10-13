"""The easy-setup inspector must never fetch pages, invent titles or send posts."""
from unittest.mock import patch
from dashboard.preview import inspect_product_link

CONFIG = {'channels': [
    {'name': 'ElectronicShelf', 'categories': ['electronics'], 'sources': ['*'], 'enabled': True},
    {'name': 'OffElectronics', 'categories': ['electronics'], 'sources': ['*'], 'enabled': False},
]}
SNAPSHOT = {'main_sources': ['deals']}
examples = [
    ({'url': 'https://www.flipkart.com/wireless-earbuds/p/itmTEST123'}, 'electronics', 'suggested'),
    ({'url': 'https://www.myntra.com/cotton-printed-kurti/123456/buy'}, 'fashion', 'suggested'),
    ({'url': 'https://www.amazon.in/rice-cooker/dp/B0TEST1234'}, 'home_kitchen', 'suggested'),
    ({'url': 'https://amzn.to/AbC123'}, None, 'needs_details'),
    ({'url': 'https://bit.ly/shoes'}, None, 'needs_details'),
    ({'url': 'https://www.amazon.in/dp/B0TEST1234'}, None, 'needs_details'),
    ({'url': 'https://www.amazon.in/dp/B0TEST1234?tag=earbuds&ref=shoes'}, None, 'needs_details'),
    ({'url': 'https://bit.ly/opaque123', 'title': 'Cotton printed kurti'}, 'fashion', 'suggested'),
    ({'url': 'https://bit.ly/opaque123', 'resolved_url': 'https://www.flipkart.com/wireless-earbuds/p/itmTEST123'}, 'electronics', 'suggested'),
    ({'url': 'https://www.flipkart.com/wireless-earbuds/p/itmTEST123', 'title': 'Cotton kurti'}, None, 'withheld'),
    ({'url': 'https://bit.ly/opaque123', 'title': 'Shoes and shoe rack'}, None, 'withheld'),
]
with patch('urllib.request.urlopen', side_effect=AssertionError('must not fetch')), \
     patch('socket.create_connection', side_effect=AssertionError('must not connect')):
    for body, category, verdict in examples:
        result = inspect_product_link(body, CONFIG, SNAPSHOT)
        assert result['category'] == category and result['verdict'] == verdict, (body, result)
        assert result['fetched'] is False
        assert 'No merchant HTML' in result['context_note']
        assert result['matching_shelves'] == (['ElectronicShelf'] if category == 'electronics' else [])
        if not category:
            assert result['matching_shelves'] == []
invalid = [None, [], {}, {'url': ''}, {'url': 12}, {'url': 'javascript:alert(1)'},
           {'url': 'file:///etc/passwd'}, {'url': 'https://<img>.com/shoes'}, {'url': 'https://-bad.example/shoes'}, {'url': 'https://'}, {'url': 'https://user:pass@example.com/shoes'},
           {'url': 'https://example.com:invalid/shoes'}, {'url': 'https://example.com/a b'},
           {'url': 'https://example.com/' + 'a'*2000},
           {'url': 'https://example.com/shoes', 'title': []},
           {'url': 'https://example.com/shoes', 'title': 'x'*2001},
           {'url': 'https://example.com/shoes', 'resolved_url': None},
           {'url': 'https://example.com/shoes', 'source': []}]
for body in invalid:
    try:
        inspect_product_link(body, CONFIG, SNAPSHOT)
    except ValueError:
        pass
    else:
        raise AssertionError(('invalid link input accepted', body))
print(f'Link inspector PASS: {len(examples)} category/abstention cases, {len(invalid)} invalid inputs, no network.')
