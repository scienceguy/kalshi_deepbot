/**
 * api/price.js
 * Vercel Serverless Function: Cross-venue spot oracle aggregator.
 * Exposes live spot prices combining RedStone, Binance, Coinbase, OKX, and GateIO.
 */

export default async function handler(req, res) {
    const { symbol = "BTC" } = req.query;
    const base = symbol.toUpperCase().replace(/(USD|USDT)$/, "");

    const results = {};
    const fetches = [
        // Binance.US
        fetch(`https://api.binance.us/api/v3/ticker/price?symbol=${base}USDT`)
            .then(r => r.json())
            .then(d => { if (d && d.price) results.binance = parseFloat(d.price); })
            .catch(() => {}),
        // Coinbase
        fetch(`https://api.exchange.coinbase.com/products/${base}-USD/ticker`)
            .then(r => r.json())
            .then(d => { if (d && d.price) results.coinbase = parseFloat(d.price); })
            .catch(() => {}),
        // OKX
        fetch(`https://www.okx.com/api/v5/market/ticker?instId=${base}-USDT`)
            .then(r => r.json())
            .then(d => { if (d && d.data && d.data[0]) results.okx = parseFloat(d.data[0].last); })
            .catch(() => {}),
        // GateIO
        fetch(`https://api.gateio.ws/api/v4/spot/tickers?currency_pair=${base}_USDT`)
            .then(r => r.json())
            .then(d => { if (d && d[0] && d[0].last) results.gateio = parseFloat(d[0].last); })
            .catch(() => {}),
    ];

    await Promise.allSettled(fetches);

    const values = Object.values(results).filter(v => typeof v === "number" && !isNaN(v)).sort((a, b) => a - b);
    let median = null;
    if (values.length > 0) {
        const mid = Math.floor(values.length / 2);
        median = values.length % 2 === 1 ? values[mid] : (values[mid - 1] + values[mid]) / 2.0;
    }

    res.status(200).json({
        symbol: base,
        median: median,
        sources: results,
        timestamp: Date.now()
    });
}
