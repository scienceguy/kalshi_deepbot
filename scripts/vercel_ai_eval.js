/**
 * scripts/vercel_ai_eval.js
 * Vercel AI SDK integration for multi-model consensus evaluation.
 * Uses Vercel AI SDK (`ai`) with providers (Groq, OpenAI, etc.).
 */

const fs = require("fs");
const path = require("path");

function loadKey(keyName) {
    const secretsDir = path.join(process.env.HOME || "", ".secrets", "bw");
    const keyFile = path.join(secretsDir, `${keyName}.json`);
    if (fs.existsSync(keyFile)) {
        try {
            const parsed = JSON.parse(fs.readFileSync(keyFile, "utf8"));
            return parsed.notes || parsed.password || "";
        } catch (_) {}
    }
    return process.env[keyName.toUpperCase()] || "";
}

async function runEval(promptText) {
    let ai, createGroq;
    try {
        ai = require("ai");
        const groqMod = require("@ai-sdk/groq");
        createGroq = groqMod.createGroq;
    } catch (e) {
        process.stderr.write("Vercel AI SDK packages not available: " + e.message + "\n");
        process.exit(1);
    }

    const groqKey = loadKey("groq_api_key");
    if (!groqKey) {
        process.stderr.write("GROQ_API_KEY not found in secrets or environment\n");
        process.exit(1);
    }

    const groq = createGroq({ apiKey: groqKey });
    try {
        const { text } = await ai.generateText({
            model: groq("qwen/qwen3.8-27b"),
            prompt: promptText,
        });
        process.stdout.write(text.trim() + "\n");
    } catch (err) {
        process.stderr.write("Vercel AI generation failed: " + err.message + "\n");
        process.exit(1);
    }
}

if (require.main === module) {
    const inputPrompt = process.argv.slice(2).join(" ") || "Analyze market condition: HOLD or PASS";
    runEval(inputPrompt);
}
