/**
 * OpenHCS Comms — pi tool adapter
 *
 * Thin shim: exposes the coordination wire as pi tool calls. Every tool
 * shells out to `agent-comms` (Python CLI) and parses JSON. This shim owns
 * no orchestration semantics; the Python core does.
 *
 * Install: copy/link into ~/.pi/agent/extensions/agent-comms/
 */

import { execFileSync } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const CLI = "agent-comms";

function run(args: string[]): Record<string, unknown> {
	try {
		const stdout = execFileSync(CLI, args, {
			encoding: "utf-8",
			timeout: 30_000,
		});
		return JSON.parse(stdout) as Record<string, unknown>;
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		// The CLI writes {"error": ...} on stdout with exit code 1.
		if (typeof (error as { stdout?: unknown }).stdout === "string") {
			try {
				const parsed = JSON.parse((error as { stdout: string }).stdout);
				if (parsed && typeof parsed.error === "string") {
					throw new Error(parsed.error);
				}
			} catch (parseError) {
				if (parseError instanceof Error && parseError.message !== "Unexpected end of JSON input") {
					throw parseError;
				}
			}
		}
		throw new Error(`agent-comms failed: ${message}`);
	}
}

export default function (pi: ExtensionAPI) {
	pi.registerTool({
		name: "comms_threads",
		label: "Comms Threads",
		description:
			"List agent threads on the coordination wire with status and pending counts.",
		parameters: Type.Object({
			active_only: Type.Optional(
				Type.Boolean({ description: "Exclude stopped threads" }),
			),
		}),
		async execute(_id, params) {
			const args = ["threads"];
			if (params.active_only) args.push("--active-only");
			const result = run(args);
			return {
				content: [
					{ type: "text", text: JSON.stringify(result, null, 2) },
				],
				details: result,
			};
		},
	});

	pi.registerTool({
		name: "comms_rename_self",
		label: "Rename Comms Thread",
		description:
			"Rename your own agent-comms thread identity (the name peers use for DMs). This does not rename the Pi or ACP session title and cannot rename another thread. Your old names remain valid permanent routing aliases.",
		parameters: Type.Object({
			new_name: Type.String({ description: "Your new thread name" }),
		}),
		async execute(_id, params) {
			const result = run(["rename-self", "--to", params.new_name]);
			return {
				content: [
					{
						type: "text",
						text: `renamed thread ${result.previous} to ${result.current}; ${result.previous} remains a routing alias`,
					},
				],
				details: result,
			};
		},
	});

	pi.registerTool({
		name: "comms_send",
		label: "Comms Send",
		description: "Send a message to another thread (or broadcast).",
		parameters: Type.Object({
			from: Type.String({ description: "Sender thread name (your PI_AGENT_ID)" }),
			to: Type.String({ description: "Target thread name or 'broadcast'" }),
			body: Type.String({ description: "Message text" }),
			type: Type.Optional(
				Type.Union([
					Type.Literal("info"),
					Type.Literal("question"),
					Type.Literal("ack"),
					Type.Literal("handoff"),
					Type.Literal("alert"),
				]),
			),
		}),
		async execute(_id, params) {
			const args = [
				"send",
				"--from", params.from,
				"--to", params.to,
				"--body", params.body,
			];
			if (params.type) args.push("--type", params.type);
			const result = run(args);
			return {
				content: [{ type: "text", text: `sent ${result.id}` }],
				details: result,
			};
		},
	});

	pi.registerTool({
		name: "comms_inbox",
		label: "Comms Inbox",
		description: "Fetch undelivered messages for a thread and mark them read.",
		parameters: Type.Object({
			thread: Type.String({ description: "Thread name (your PI_AGENT_ID)" }),
			ack: Type.Optional(
				Type.Boolean({ description: "Mark delivered after reading (default true)" }),
			),
		}),
		async execute(_id, params) {
			const inbox = run(["inbox", "--thread", params.thread]);
			if (params.ack !== false) run(["ack", "--thread", params.thread]);
			const messages = (inbox as { messages?: unknown[] }).messages ?? [];
			const text =
				messages.length === 0
					? "inbox empty"
					: messages
							.map((m) => {
								const msg = m as Record<string, unknown>;
								return `#${msg.seq} [${msg.type}] ${msg.from}: ${msg.text}`;
							})
							.join("\n");
			return { content: [{ type: "text", text }], details: inbox };
		},
	});

	pi.registerTool({
		name: "comms_fork",
		label: "Comms Fork",
		description:
			"Fork a child pi thread from a parent's session. Parent must be registered with a session file.",
		parameters: Type.Object({
			name: Type.String({ description: "Child thread name" }),
			parent: Type.String({ description: "Parent thread name" }),
			task: Type.String({ description: "Task for the child" }),
			tags: Type.Optional(Type.String({ description: "Comma-separated tags" })),
			prompt: Type.Optional(Type.String({ description: "Initial prompt override" })),
		}),
		async execute(_id, params) {
			const args = [
				"fork",
				"--name", params.name,
				"--parent", params.parent,
				"--task", params.task,
			];
			if (params.tags) args.push("--tags", params.tags);
			if (params.prompt) args.push("--prompt", params.prompt);
			const result = run(args);
			return {
				content: [
					{
						type: "text",
						text: `forked ${result.forked} (pid ${result.pid})`,
					},
				],
				details: result,
			};
		},
	});
}
