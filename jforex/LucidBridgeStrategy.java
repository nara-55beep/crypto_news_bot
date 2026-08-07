/*
 * LucidBridgeStrategy.java
 *
 * JForex strategy that forwards exact Dukascopy proxy ticks into the local
 * Lucid bridge receiver at http://127.0.0.1:8765/tick.
 *
 * Run this inside JForex or JForex SDK with subscriptions to:
 *   USA500IDXUSD  -> es
 *   USATECHIDXUSD -> nq
 *   LIGHTCMDUSD   -> cl
 *
 * It does not trade. It only forwards ticks.
 */

import com.dukascopy.api.IAccount;
import com.dukascopy.api.IBar;
import com.dukascopy.api.IContext;
import com.dukascopy.api.IMessage;
import com.dukascopy.api.IStrategy;
import com.dukascopy.api.ITick;
import com.dukascopy.api.Instrument;
import com.dukascopy.api.JFException;
import com.dukascopy.api.Period;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

public class LucidBridgeStrategy implements IStrategy {
    private static final String DEFAULT_ENDPOINT = "http://127.0.0.1:8765/tick";
    private final Map<Instrument, String> marketByInstrument = new HashMap<Instrument, String>();
    private String endpoint;
    private String token;

    public LucidBridgeStrategy() {
        marketByInstrument.put(Instrument.USA500IDXUSD, "es");
        marketByInstrument.put(Instrument.USATECHIDXUSD, "nq");
        marketByInstrument.put(Instrument.LIGHTCMDUSD, "cl");
    }

    public void onStart(IContext context) throws JFException {
        endpoint = System.getProperty("lucid.bridge.endpoint", DEFAULT_ENDPOINT);
        token = System.getProperty("lucid.bridge.token", "");
        Set<Instrument> instruments = new HashSet<Instrument>(Arrays.asList(
            Instrument.USA500IDXUSD,
            Instrument.USATECHIDXUSD,
            Instrument.LIGHTCMDUSD
        ));
        context.setSubscribedInstruments(instruments, true);
        context.getConsole().getOut().println("Lucid bridge forwarding ticks to " + endpoint);
    }

    public void onTick(Instrument instrument, ITick tick) throws JFException {
        String market = marketByInstrument.get(instrument);
        if (market == null) {
            return;
        }
        double bid = tick.getBid();
        double ask = tick.getAsk();
        double volume = Math.max(0.0, tick.getBidVolume()) + Math.max(0.0, tick.getAskVolume());
        String json = "{"
            + "\"market\":\"" + market + "\","
            + "\"dt_utc\":\"" + Instant.ofEpochMilli(tick.getTime()).toString() + "\","
            + "\"bid\":" + bid + ","
            + "\"ask\":" + ask + ","
            + "\"volume\":" + volume
            + "}";
        post(json);
    }

    private void post(String json) {
        HttpURLConnection conn = null;
        try {
            URL url = new URL(endpoint);
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(1000);
            conn.setReadTimeout(1000);
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json");
            if (token != null && !token.isEmpty()) {
                conn.setRequestProperty("X-Lucid-Bridge-Token", token);
            }
            byte[] body = json.getBytes(StandardCharsets.UTF_8);
            conn.setRequestProperty("Content-Length", Integer.toString(body.length));
            OutputStream out = conn.getOutputStream();
            try {
                out.write(body);
            } finally {
                out.close();
            }
            int code = conn.getResponseCode();
            if (code < 200 || code >= 300) {
                System.err.println("Lucid bridge POST failed HTTP " + code + ": " + json);
            }
        } catch (Exception e) {
            System.err.println("Lucid bridge POST error: " + e.getClass().getSimpleName() + ": " + e.getMessage());
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    public void onBar(Instrument instrument, Period period, IBar askBar, IBar bidBar) throws JFException {
    }

    public void onMessage(IMessage message) throws JFException {
    }

    public void onAccount(IAccount account) throws JFException {
    }

    public void onStop() throws JFException {
    }
}
