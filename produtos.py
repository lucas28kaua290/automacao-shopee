import time
import hashlib
import json
import requests

app_id = "18344090536"
secret = "3IUMPZ7A5BZ7JG45KLIHLUOTH5K2LML4"
url = "https://open-api.affiliate.shopee.com.br/graphql"

query = """
    {
        productOfferV2(
            keyword: "smartphone",
            listType: 1,
            sortType: 2,
            page: 1,
            limit: 5
        ) {
            nodes {
                itemId
                productName
                imageUrl
                offerLink
                priceMin
                commissionRate
                sellerCommissionRate
                shopeeCommissionRate
                commission
            }
            pageInfo {
                page
                limit
                hasNextPage
            }
        }
    }
"""

payload = {"query" : query}
payload_json = json.dumps(payload)

timestamp = int(time.time())
base_string = f"{app_id}{timestamp}{payload_json}{secret}"
signature = hashlib.sha256(base_string.encode('utf-8')).hexdigest()

headers = {
    "Content-Type": "application/json",
    "Authorization": f"SHA256 Credential={app_id}, Timestamp={timestamp}, Signature={signature}"
}

response = requests.post(url, headers=headers, data=payload_json)

print(f"Status Code: {response.status_code}")
print("Resposta: ")
print(json.dumps(response.json(), indent=2, ensure_ascii=False))