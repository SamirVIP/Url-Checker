const axios = require("axios");
const TelegramBot = require("node-telegram-bot-api");
const nodemailer = require("nodemailer");
const fs = require("fs");
const path = require("path");

// ================= CONFIG =================
const VERSION_START = Number(process.env.VERSION_START || 20);
const VERSION_END = Number(process.env.VERSION_END || 30);
const VERSION_BASE = process.env.VERSION_BASE || "1.126";
const RESOURCE_START = Number(process.env.RESOURCE_START || 90);
const RESOURCE_END = Number(process.env.RESOURCE_END || 100);
const ICON_START = Number(process.env.ICON_START || 710055001);
const ICON_END = Number(process.env.ICON_END || 710055011);
const SCAN_INTERVAL_MS = Number(process.env.SCAN_INTERVAL_MS || 5000);
const REQUEST_TIMEOUT_MS = Number(process.env.REQUEST_TIMEOUT_MS || 8000);
const ALERT_RETRIES = Number(process.env.ALERT_RETRIES || 2);

// ================= TELEGRAM =================
const CHAT_IDS = [process.env.CHAT_ID, process.env.CHAT_ID2]
    .filter(Boolean)
    .flatMap((value) => value.split(",").map((id) => id.trim()).filter(Boolean));

const bot = process.env.BOT_TOKEN ? new TelegramBot(process.env.BOT_TOKEN) : null;

// ================= EMAIL =================
const emailRecipients = [process.env.EMAIL_TO, process.env.EMAIL_TO2]
    .filter(Boolean)
    .flatMap((value) => value.split(",").map((email) => email.trim()).filter(Boolean));

const transporter = (process.env.EMAIL_USER && process.env.EMAIL_PASS)
    ? nodemailer.createTransport({
        service: process.env.EMAIL_SERVICE || "gmail",
        auth: {
            user: process.env.EMAIL_USER,
            pass: process.env.EMAIL_PASS
        }
    })
    : null;

// ================= STATE =================
const STATE_FILE = path.join(__dirname, "notified.json");
let notified = {};

if (fs.existsSync(STATE_FILE)) {
    try {
        notified = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
    } catch {
        notified = {};
    }
}

function saveState() {
    fs.writeFileSync(STATE_FILE, JSON.stringify(notified, null, 2));
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function buildAlert(name, url) {
    const time = new Date().toLocaleString("en-IN", {
        timeZone: process.env.TIMEZONE || "Asia/Kolkata",
        dateStyle: "medium",
        timeStyle: "medium"
    });

    return {
        name,
        url,
        time,
        telegram: [
            "🚨 <b>Free Fire CDN Update Found</b>",
            "",
            `<b>Name:</b> ${escapeHtml(name)}`,
            `<b>Status:</b> 200 OK`,
            
            `<b>URL:</b> ${escapeHtml(url)}`,
            `<b>Time:</b> ${escapeHtml(time)}`
        ].join("\n"),
        text: [
            "Free Fire CDN Update Found",
            "",
            `Name: ${name}`,
            "Status: 200 OK",
           
            `URL: ${url}`,
            `Time: ${time}`
        ].join("\n"),
        html: `
            <h2>🚨 Free Fire CDN Update Found</h2>
            <table cellpadding="6" cellspacing="0" border="0">
                <tr><td><b>Name</b></td><td>${escapeHtml(name)}</td></tr>
                <tr><td><b>Status</b></td><td>200 OK</td></tr>
                
                <tr><td><b>URL</b></td><td><a href="${escapeHtml(url)}">${escapeHtml(url)}</a></td></tr>
                <tr><td><b>Time</b></td><td>${escapeHtml(time)}</td></tr>
            </table>
        `
    };
}

async function retry(fn, label) {
    let lastError;
    for (let attempt = 0; attempt <= ALERT_RETRIES; attempt++) {
        try {
            return await fn();
        } catch (error) {
            lastError = error;
            if (attempt < ALERT_RETRIES) {
                const delay = 1000 * (attempt + 1);
                console.log(`${label} retry ${attempt + 1}/${ALERT_RETRIES} in ${delay}ms`);
                await new Promise((resolve) => setTimeout(resolve, delay));
            }
        }
    }
    throw lastError;
}

// ================= ALERTS =================
async function sendTelegram(alert) {
    if (!bot || CHAT_IDS.length === 0) {
        console.log("Telegram skipped: BOT_TOKEN/CHAT_ID not configured");
        return false;
    }

    const results = await Promise.allSettled(
        CHAT_IDS.map((chatId) => retry(
            () => bot.sendMessage(chatId, alert.telegram, { parse_mode: "HTML", disable_web_page_preview: false }),
            `Telegram ${chatId}`
        ))
    );

    let success = false;
    results.forEach((result, index) => {
        if (result.status === "fulfilled") {
            success = true;
            console.log(`Telegram sent: ${CHAT_IDS[index]} -> ${alert.name}`);
        } else {
            console.error(`Telegram failed: ${CHAT_IDS[index]} -> ${result.reason?.message || result.reason}`);
        }
    });
    return success;
}

async function sendEmail(alert) {
    if (!transporter || emailRecipients.length === 0) {
        console.log("Email skipped: EMAIL_USER/EMAIL_PASS/EMAIL_TO not configured");
        return false;
    }

    try {
        await retry(
            () => transporter.sendMail({
                from: process.env.EMAIL_FROM || process.env.EMAIL_USER,
                to: emailRecipients.join(", "),
                subject: `🚨 Free Fire CDN Found: ${alert.name}`,
                text: alert.text,
                html: alert.html
            }),
            "Email"
        );
        console.log(`Email sent: ${emailRecipients.join(", ")} -> ${alert.name}`);
        return true;
    } catch (error) {
        console.error(`Email failed: ${error.message}`);
        return false;
    }
}

async function sendAlert(name, url) {
    const alert = buildAlert(name, url);
    console.log(`Sending alert: ${name}`);

    const [telegramOk, emailOk] = await Promise.all([
        sendTelegram(alert),
        sendEmail(alert)
    ]);

    // Only mark it as notified when at least one channel delivered successfully.
    // If all channels fail, the next scan can retry the alert.
    if (telegramOk || emailOk) {
        notified[name] = {
            notifiedAt: new Date().toISOString(),
            telegram: telegramOk,
            email: emailOk,
            url
        };
        saveState();
        console.log(`Alert complete: ${name} | Telegram=${telegramOk} Email=${emailOk}`);
        return true;
    }

    console.error(`All alert channels failed: ${name}`);
    return false;
}

// ================= CHECK URL =================
async function checkURL(name, url) {
    try {
        const res = await axios.get(url, {
            timeout: REQUEST_TIMEOUT_MS,
            validateStatus: () => true,
            headers: {
                "User-Agent": "URL-Checker/2.0"
            }
        });

        console.log(`${name} ${res.status}`);

        if (res.status === 200 && !notified[name]) {
            console.log(`NEW FOUND: ${name}`);
            await sendAlert(name, url);
        }
    } catch (error) {
        console.log(`${name} ERROR: ${error.message}`);
    }
}

// ================= SCANS =================
async function scanVersion() {
    console.log("VERSION SCAN");
    const jobs = [];
    for (let i = VERSION_START; i <= VERSION_END; i++) {
        const version = `${VERSION_BASE}.${i}`;
        jobs.push(checkURL(version, `https://dl.cdn.freefiremobile.com/live/ABHotUpdates/android/${version}/fileinfo`));
    }
    await Promise.all(jobs);
}

async function scanResource() {
    console.log("RESOURCE SCAN");
    const jobs = [];
    for (let i = RESOURCE_START; i <= RESOURCE_END; i++) {
        const name = `optionallocres-${i}`;
        jobs.push(checkURL(name, `https://dl.cdn.freefiremobile.com/advance/ABHotUpdates/android/optional/optionallocres/${i}/fileinfo`));
    }
    await Promise.all(jobs);
}

async function scanIcon() {
    console.log("ICON SCAN");
    const jobs = [];
    for (let i = ICON_START; i <= ICON_END; i++) {
        jobs.push(checkURL(`LIVE-ICON-${i}`, `https://dl.cdn.freefiremobile.com/live/ABHotUpdates/IconCDN/android/${i}_rgb.astc`));
        jobs.push(checkURL(`ADVANCE-ICON-${i}`, `https://dl.cdn.freefiremobile.com/advance/ABHotUpdates/IconCDN/android/${i}_rgb.astc`));
    }
    await Promise.all(jobs);
}

// ================= MAIN =================
let running = false;

async function monitor() {
    if (running) {
        console.log("Previous scan still running...");
        return;
    }

    running = true;
    console.log("\n==========================");
    console.log("Checking...", new Date().toLocaleString("en-IN", { timeZone: process.env.TIMEZONE || "Asia/Kolkata" }));

    try {
        await scanVersion();
        await scanResource();
        await scanIcon();
        console.log("Scan Complete");
    } catch (error) {
        console.error("Monitor Error:", error.message);
    } finally {
        running = false;
    }
}

monitor();
setInterval(monitor, SCAN_INTERVAL_MS);
