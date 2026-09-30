# BOOP screenshots

The images in this folder are captures of the actual BOOP interface against a disposable, synthetic dataset. They do not contain personal health records, real strap identifiers or credentials. The visible demo label distinguishes the preview from a connected strap.

## Reproduce the preview

After installing the development dependencies with `setup.ps1`, run:

```powershell
.\.venv\Scripts\python.exe .\tools\qa_server.py --demo --port 8767
```

Open [127.0.0.1:8767](http://127.0.0.1:8767/). The preview seeds its own temporary database and never opens a Bluetooth connection. It does not load or modify the app's recording database. Hardware and OS actions remain disabled.

Allow roughly a minute for the first analytics calculation to finish. The current HR, battery and live RR examples are simulated. Saved charts, sleep stages and daily scores use the normal data and analytics pipelines.

The screenshots cover Today, Sleep, Activity, Health with dark appearance, and breathing tools. Capture the normal browser viewport after each page finishes loading. Appearance changes apply only to the disposable preview. The database is removed when the preview quits.

For the Activity view, open History and a workout's HR & recovery detail. For the Tools view, start a breathing session with sounds and haptics unchecked. Use Settings to choose dark appearance for the Health view.

Daily scores and charts are calculated by BOOP's existing analytics from the synthetic records; unknown values retain their normal explanations. Demo values illustrate the interface and are not measurements or clinical claims.
