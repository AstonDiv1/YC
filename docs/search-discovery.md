# Search discovery for Numéryl

Google's domain property is verified through DNS, the homepage is indexed, and the sitemap has been read successfully. Indexing does not guarantee a position for a particular search query.

## IndexNow ownership endpoint

The exact `/indexnow-key.txt` route serves only the `INDEXNOW_KEY` value when it contains 8–128 letters, digits or hyphens. It returns an empty 404 when the value is missing or invalid. No other environment variables or files are exposed.

The owner authorized minor visibility and acquisition improvements on 11 October 2026. This route supports a single notification of the public homepage to compatible engines after deployment and HTTPS verification. It does not create accounts, buy services or alter website prices, mail settings or form behavior.

Activation and validation:

1. Set a dedicated ownership key in `INDEXNOW_KEY` on the existing Render service, merging rather than replacing other variables. Keep it out of chat, PR text and this document. It is a public website ownership proof, not a mail or hosting API credential.
2. Deploy and compare the HTTPS key file against the configured value.
3. Notify a participating search engine using `host: numeryl.fr`, the key, `keyLocation: https://numeryl.fr/indexnow-key.txt` and `urlList: [https://numeryl.fr/]`.
4. Record the actual response. HTTP 200 means received; 202 means ownership verification pending. Neither confirms indexing or ranking.
5. Submit again only after a real page update. Do not include fictive demos, private/admin routes or customer information.

No automatic submission runs inside the app. Google remains managed through Search Console.

Official protocol: https://www.indexnow.org/documentation
