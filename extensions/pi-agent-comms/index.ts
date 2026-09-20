/**
 * OpenHCS Comms — pi tool adapter
 *
 * Tool names, schemas, descriptions, and invocation behavior come from the
 * Python tool catalog. This adapter only translates the catalog into pi tools.
 */

import { execFileSync } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const CLI = "agent-comms";

interface ToolDeclaration {
	name: string;
	label: string;
	description: string;
	parameters: Record<string, unknown>;
}

function run(args: string[]): Record<string, unknown> {
	try {
		const stdout = execFileSync(CLI, args, {
			encoding: "utf-8",
			timeout: 30_000,
		});
		return JSON.parse(stdout) as Record<string, unknown>;
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		if (typeof (error as { stdout?: unknown }).stdout === "string") {
			try {
				const parsed = JSON.parse((error as { stdout: string }).stdout);
				if (parsed && typeof parsed.error === "string") {
					throw new Error(parsed.error);
				}
			} catch (parseError) {
				if (
					parseError instanceof Error &&
					parseError.message !== "Unexpected end of JSON input"
				) {
					throw parseError;
				}
			}
		}
		throw new Error(`agent-comms failed: ${message}`);
	}
}

export default function (pi: ExtensionAPI) {
	const catalog = run(["tools"]).tools as ToolDeclaration[];
	for (const declaration of catalog) {
		pi.registerTool({
			name: declaration.name,
			label: declaration.label,
			description: declaration.description,
			parameters: Type.Unsafe(declaration.parameters),
			async execute(_id, params) {
				const result = run([
					"invoke",
					"--tool",
					declaration.name,
					"--arguments",
					JSON.stringify(params),
				]);
				return {
					content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
					details: result,
				};
			},
		});
	}
}
