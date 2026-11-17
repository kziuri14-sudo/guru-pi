package lt.guru.app;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/** Lažybų analitika: atidaro Raspberry puslapį. Dabartinį (kintantį) adresą suranda pati – assets/index.html. */
public class MainActivity extends Activity {
    private WebView web;
    private long pausedAt = 0;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0B0D12"));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setAllowUniversalAccessFromFileURLs(true);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);   // visada naujausias Raspberry puslapis
        web.clearCache(true);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) {
                Uri u = req.getUrl();
                String h = u.getHost() == null ? "" : u.getHost();
                if ("file".equals(u.getScheme()) || h.endsWith("trycloudflare.com")) return false;
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (Exception e) { }
                return true;                       // kazino ir kitos nuorodos – naršyklėje
            }
        });
        setContentView(web);
        if (state != null) web.restoreState(state);
        else web.loadUrl("file:///android_asset/index.html");
    }

    @Override
    protected void onPause() { super.onPause(); pausedAt = System.currentTimeMillis(); }

    @Override
    protected void onResume() {
        super.onResume();
        // grįžus po ilgesnės pertraukos – iš naujo surandam Raspberry adresą (jis galėjo pasikeisti)
        if (pausedAt > 0 && System.currentTimeMillis() - pausedAt > 10 * 60 * 1000) {
            web.clearCache(true);
            web.loadUrl("file:///android_asset/index.html");
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle out) { super.onSaveInstanceState(out); web.saveState(out); }

    @Override
    public void onBackPressed() {
        if (web.canGoBack() && !web.getUrl().startsWith("file:")) web.goBack();
        else super.onBackPressed();
    }
}
