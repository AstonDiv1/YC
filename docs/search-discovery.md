# Search discovery for Numéryl

Google's domain property is verified through DNS, the homepage is indexed, and the sitemap has been read successfully. Indexing does not guarantee a position for a particular search query.

## Optional IndexNow activation

This change is prepared for review. It does not submit URLs, create accounts, purchase services, or activate a new key automatically.

The exact `/indexnow-key.txt` route serves only the `INDEXNOW_KEY` value when it contains 8–128 letters, digits or hyphens. It returns an empty 404 when the value is missing or invalid. No other environment variables or files are exposed.

After the owner approves activation:

1. Set a dedicated random ownership key in `INDEXNOW_KEY` on the existing Render service, without replacing other variables. Keep it out of chat, PR text and this document. The key is a public website ownership proof, not a mail or hosting API credential.
2. Deploy and check the HTTPS key file against the exact configured value.
3. Notify a participating search engine using `host: numeryl.fr`, the ownership key, `keyLocation: https://numeryl.fr/indexnow-key.txt` and `urlList: [https://numeryl.fr/]`.
4. Record the actual response. HTTP 200 means received; 202 means ownership verification pending. Neither confirms indexing or ranking.
5. Submit again only after a real page update, not repeatedly to force a ranking. Do not include the fictive demo, private/admin routes or customer information.

The owner requested that critical decisions wait until their return. Do not activate or change production configuration as part of this draft.

Official protocol: https://www.indexnow.org/documentation
Google's search-operator limitations: https://developers.google.com/search/docs/monitor-debug/search-operators/all-search-site
