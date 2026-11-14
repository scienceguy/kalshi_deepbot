/**
 * scripts/redstone_provider.js
 * RedStone oracle bridge using redstone-api (https://github.com/redstone-finance/redstone-api)
 * Supports configurable cacheApiUrl, Vercel edge/proxy, and local fallback.
 */

const fs = require("fs");

async function main() {
    const symbol = (process.argv[2] || "BTC").toUpperCase();
    let redstone;
    try {
        redstone = require("redstone-api");
    } catch (err) {
        process.stderr.write("redstone-api module not found: " + err.message + "\n");
        process.exit(1);
    }

    const cacheUrl = process.env.REDSTONE_CACHE_URL || process.env.VERCEL_REDSTONE_URL;
    if (cacheUrl) {
        try {
            redstone.setCacheApiUrl(cacheUrl);
        } catch (_) {}
    }

    try {
        const price = await redstone.getPrice(symbol);
        if (price && typeof price.value === "number") {
            process.stdout.write(JSON.stringify({
                symbol: symbol,
                value: price.value,
                timestamp: price.timestamp,
                source: "redstone"
            }) + "\n");
            process.exit(0);
        } else {
            throw new Error("Empty price data returned for " + symbol);
        }
    } catch (e) {
        process.stderr.write("RedStone fetch failed for " + symbol + ": " + e.message + "\n");
        process.exit(1);
    }
}

if (require.main === module) {
    main();
}
