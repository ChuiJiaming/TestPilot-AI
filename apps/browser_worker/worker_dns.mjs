import net from "node:net";
import { Resolver, lookup as systemLookup } from "node:dns/promises";

const dnsServer = process.env.TESTPILOT_BROWSER_WORKER_DNS_SERVER?.trim();
const resolver = new Resolver();

if (dnsServer) {
  if (net.isIP(dnsServer) !== 4) {
    throw new Error("TESTPILOT_BROWSER_WORKER_DNS_SERVER must be an IPv4 address");
  }
  resolver.setServers([dnsServer]);
}

function isMissingRecord(error) {
  return error?.code === "ENODATA" || error?.code === "ENOTFOUND";
}

async function resolveRecord(hostname, family) {
  try {
    const addresses = family === 4
      ? await resolver.resolve4(hostname)
      : await resolver.resolve6(hostname);
    return addresses.map((address) => ({ address, family }));
  } catch (error) {
    if (isMissingRecord(error)) return [];
    throw error;
  }
}

export async function resolveWorkerHostname(hostname) {
  if (net.isIP(hostname)) {
    return [{ address: hostname, family: net.isIP(hostname) }];
  }
  if (!dnsServer) return systemLookup(hostname, { all: true, verbatim: true });

  const [ipv4, ipv6] = await Promise.all([
    resolveRecord(hostname, 4),
    resolveRecord(hostname, 6),
  ]);
  const answers = [...ipv4, ...ipv6];
  if (!answers.length) throw Object.assign(new Error("DNS_NO_ADDRESS"), { code: "ENOTFOUND" });
  return answers;
}
