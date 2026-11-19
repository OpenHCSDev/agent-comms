import { isAbsolute } from 'node:path';
import { getAgentDir } from '@earendil-works/pi-coding-agent';

/** MCP declarations and grants belong to the original user profile, not writable session resources. */
export function getMcpAgentDir() {
  const directory = process.env.AGENT_COMMS_NATIVE_CONFIG_DIR ?? getAgentDir();
  if (!isAbsolute(directory)) throw new Error('Absolute MCP user configuration directory required');
  return directory;
}
