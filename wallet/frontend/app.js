const apiBase = "http://127.0.0.1:5000";

const fileInput = document.getElementById("walletFile");
const passwordInput = document.getElementById("password");
const fromInput = document.getElementById("fromAddr");
const toInput = document.getElementById("toAddr");
const amountInput = document.getElementById("amount");
const feeInput = document.getElementById("fee");
const output = document.getElementById("output");
const walletStatus = document.getElementById("walletStatus");

let lastSigned = null;
let currentWallet = null;

// Update wallet status display
function updateWalletStatus(walletInfo = null) {
  if (walletInfo) {
    walletStatus.className = "wallet-status";
    walletStatus.innerHTML = `✅ <strong>Active Wallet:</strong> ${walletInfo.filename}<br>
                              📍 <strong>Addresses:</strong> ${walletInfo.addresses.join(', ')}`;
  } else {
    walletStatus.className = "wallet-status no-wallet";
    walletStatus.innerHTML = "⚠️ No wallet loaded. Upload a wallet to sign transactions.";
  }
}

// Get wallet info from server
async function loadWalletInfo() {
  try {
    const res = await fetch(`${apiBase}/api/current_wallet`);
    if (res.ok) {
      const data = await res.json();
      currentWallet = data;
      updateWalletStatus(data);
    } else {
      currentWallet = null;
      updateWalletStatus(null);
    }
  } catch (err) {
    console.error("Failed to load wallet info:", err);
    currentWallet = null;
    updateWalletStatus(null);
  }
}

// Upload wallet file
fileInput.addEventListener("change", async () => {
  const file = fileInput.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    const res = await fetch(`${apiBase}/api/upload_wallet`, {
      method: "POST",
      body: form,
    });
    const data = await res.json();
    if (res.ok) {
      output.textContent = "✅ Wallet uploaded.";
      // Load wallet info to update status
      await loadWalletInfo();
    } else {
      output.textContent = "❌ Upload failed: " + (data.error || "");
    }
  } catch (err) {
    output.textContent = "❌ Error: " + err.message;
  }
});

// CREATE WALLET
document.getElementById("createWalletBtn").addEventListener("click", async () => {
  const password = document.getElementById("newPassword").value;
  const label = document.getElementById("newLabel").value || "My Wallet";
  const filename = document.getElementById("newFilename").value;

  if (!password) {
    output.textContent = "⚠️ Enter a password.";
    return;
  }

  output.textContent = "⏳ Creating wallet...";

  try {
    const res = await fetch(`${apiBase}/api/create_wallet`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password, label, filename }),
    });

    const data = await res.json();

    if (!res.ok) {
      output.textContent = "❌ " + (data.error || "Failed to create wallet.");
      return;
    }

    output.textContent = `✅ Wallet created!\n\nAddress: ${data.address}\nFile: ${data.filename}`;

    // Download wallet file
    //const fileRes = await fetch(`${apiBase}/wallet_files/${data.filename}`);
    //if (!fileRes.ok) throw new Error("Failed to fetch wallet file");
    //const blob = await fileRes.blob();
    //const link = document.createElement("a");
    //link.href = URL.createObjectURL(blob);
    //link.download = data.filename;
    //link.click();

    // Automatically set the newly created wallet as active
    const formData = new FormData();
    const walletFile = new File([blob], data.filename, { type: "application/json" });
    formData.append("file", walletFile);
    await fetch(`${apiBase}/api/upload_wallet`, { method: "POST", body: formData });
    await loadWalletInfo();

  } catch (err) {
    output.textContent = "❌ Error: " + err.message;
  }
});

// Sign transaction
document.getElementById("signBtn").addEventListener("click", async () => {
  const tx = {
    txin: fromInput.value.trim(),
    txout: toInput.value.trim(),
    amount: parseFloat(amountInput.value),
    fee: parseFloat(feeInput.value),
  };

  const password = passwordInput.value;
  if (!tx.txin || !tx.txout || !password) {
    output.textContent = "⚠️ Fill in all fields first.";
    return;
  }

  if (!currentWallet) {
    output.textContent = "⚠️ No wallet loaded. Upload a wallet first.";
    return;
  }

  // Validate that the sender address belongs to the current wallet
  if (!currentWallet.addresses.includes(tx.txin)) {
    output.textContent = `⚠️ Sender address ${tx.txin} not found in current wallet.\nAvailable addresses: ${currentWallet.addresses.join(', ')}`;
    return;
  }

  const body = {
    transactions: [tx],
    password,
  };

  output.textContent = "⏳ Signing transaction...";
  try {
    const res = await fetch(`${apiBase}/api/sign_transactions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    const data = await res.json();
    if (res.ok) {
      lastSigned = data;
      output.textContent = "✅ Transaction signed:\n\n" + JSON.stringify(data, null, 2);
    } else {
      output.textContent = "❌ " + (data.error || "Signing failed");
    }
  } catch (err) {
    output.textContent = "❌ Error: " + err.message;
  }
});

// Send signed transaction to node
document.getElementById("sendBtn").addEventListener("click", async () => {
  if (!lastSigned) {
    output.textContent = "⚠️ Sign a transaction first.";
    return;
  }

  try {
    const res = await fetch(`${apiBase}/api/send_transactions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ signed: lastSigned }),
    });

    const data = await res.json();
    if (res.ok) {
      output.textContent = "✅ Sent to node: " + JSON.stringify(data);
    } else {
      output.textContent = "❌ " + (data.error || "Send failed");
    }
  } catch (err) {
    output.textContent = "❌ Error: " + err.message;
  }
});

// Generate new key
document.getElementById("generateKeyBtn").addEventListener("click", async () => {
  const walletFile = document.getElementById("walletFile").files[0];
  const password = document.getElementById("password").value;

  if (!walletFile || !password) {
      output.textContent = "⚠️ Upload wallet and enter password first.";
      return;
  }

  // Ask for label
  const label = prompt("Enter a label for the new key (optional):") || "";

  try {
    // Upload wallet to backend
    const formData = new FormData();
    formData.append("file", walletFile);
    await fetch(`${apiBase}/api/upload_wallet`, { method: "POST", body: formData });

    // Call add_key endpoint
    const res = await fetch(`${apiBase}/api/add_key`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            wallet_file: walletFile.name,
            password,
            label
        })
    });

    const data = await res.json();
    if (res.ok) {
        output.textContent = `✅ New key added!\nAddress: ${data.address}\nLabel: ${data.label}`;
        // Reload wallet info to show new address
        await loadWalletInfo();
    } else {
        output.textContent = `❌ Error: ${data.error}`;
    }
  } catch (err) {
    output.textContent = "❌ Error: " + err.message;
  }
});

// Load wallet info on page load
loadWalletInfo();