# Collection policy

bankiru-reviews collects negative reviews (ratings 1 and 2) from banki.ru.
Each stored review has datePublished, reviewBody, bankName, url, location,
and product.

Readers are Authentik users of the UI, holders of API_TOKEN, and holders of
a guest token. An anonymous request that arrives with X-Bankiru-Gateway: 1
and no API-Token receives 403. This work does not add readers and does not
remove guest tokens.

Each run sends one User-Agent:

bankiru-reviews/0.1 (+https://bankiru.uva-advanced.ru; operator=uva-advanced)

The run reads https://www.banki.ru/robots.txt from the same egress as the
crawl. The file fetched on 2026-11-01 allows the listing path and the detail
path for User-agent *. It disallows a query containing ?f[ or &f[ under
/services/responses/, and it disallows a detail URL's /comments/ path.

This page does not say that the operator has a license from banki.ru.
Keeping the collection is decision D9, recorded 2026-11-02.
