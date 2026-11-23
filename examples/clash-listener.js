// Use as a Clash Verge Rev merge script after creating a Claude-Egress group.
function main(config) {
  const group = "Claude-Egress";
  const name = "claude-isolation";
  const port = 17880;
  const others = (config.listeners || []).filter((listener) => listener.name !== name);
  if (!(config["proxy-groups"] || []).some((entry) => entry.name === group)) {
    config.listeners = others;
    return config;
  }
  if (others.some((listener) => Number(listener.port) === port)) {
    throw new Error("Choose a dedicated, unused port for Claude Guard.");
  }
  config.listeners = [
    ...others,
    { name, type: "mixed", listen: "127.0.0.1", port, proxy: group, udp: false },
  ];
  return config;
}
