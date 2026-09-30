/** External request schemas projected into Pi's existing text/image estimator.
 * No token formulas, recursive JSON string walker, model policy, or saved state.
 */
export class RequestInputError extends Error {}

class WireBlock {
    static decode(block) {
        const declaration = this.declarations.find(candidate => candidate.matches(block));
        if (declaration === undefined) throw new RequestInputError("Unsupported request input block");
        return declaration.content(block);
    }
}
const text = value => ({ type: "text", text: value });
const image = () => ({ type: "image" });
const message = content => ({ role: "user", content, timestamp: 0 });
const stringContent = value => typeof value === "string" ? [text(value)] : undefined;

class ChatBlock extends WireBlock { static declarations = []; }
class ChatText extends ChatBlock {
    static { ChatBlock.declarations.push(this); }
    static matches(block) { return block.type === "text"; }
    static content(block) { return [text(block.text)]; }
}
class ChatImage extends ChatBlock {
    static { ChatBlock.declarations.push(this); }
    static matches(block) { return block.type === "image_url"; }
    static content() { return [image()]; }
}
function chatContent(content) {
    if (content === null || content === undefined) return [];
    return stringContent(content) ?? content.flatMap(block => ChatBlock.decode(block));
}
export function chatInput(params) {
    return { tools: params.tools, messages: params.messages.map(row => message([
        ...chatContent(row.content),
        ...(row.reasoning_content ? [text(row.reasoning_content)] : []),
        ...(row.tool_calls ? [text(JSON.stringify(row.tool_calls))] : []),
    ])) };
}

class AnthropicBlock extends WireBlock { static declarations = []; }
class AnthropicText extends AnthropicBlock {
    static { AnthropicBlock.declarations.push(this); }
    static matches(block) { return block.type === "text"; }
    static content(block) { return [text(block.text)]; }
}
class AnthropicImage extends AnthropicBlock {
    static { AnthropicBlock.declarations.push(this); }
    static matches(block) { return block.type === "image"; }
    static content() { return [image()]; }
}
class AnthropicThinking extends AnthropicBlock {
    static { AnthropicBlock.declarations.push(this); }
    static matches(block) { return block.type === "thinking"; }
    static content(block) { return [text(block.thinking)]; }
}
class AnthropicRedactedThinking extends AnthropicBlock {
    static { AnthropicBlock.declarations.push(this); }
    static matches(block) { return block.type === "redacted_thinking"; }
    // Opaque signed material is not visible text. Native measured prefix usage
    // remains with estimateContextTokens, not a ciphertext length heuristic.
    static content() { return []; }
}
class AnthropicToolUse extends AnthropicBlock {
    static { AnthropicBlock.declarations.push(this); }
    static matches(block) { return block.type === "tool_use"; }
    static content(block) { return [text(block.name + JSON.stringify(block.input))]; }
}
class AnthropicToolResult extends AnthropicBlock {
    static { AnthropicBlock.declarations.push(this); }
    static matches(block) { return block.type === "tool_result"; }
    static content(block) { return anthropicContent(block.content); }
}
function anthropicContent(content) {
    return stringContent(content) ?? content.flatMap(block => AnthropicBlock.decode(block));
}
export function anthropicInput(params) {
    return { tools: params.tools, messages: [
        ...(params.system ? [message(anthropicContent(params.system))] : []),
        ...params.messages.map(row => message(anthropicContent(row.content))),
    ] };
}

class ResponseBlock extends WireBlock { static declarations = []; }
class ResponseText extends ResponseBlock {
    static { ResponseBlock.declarations.push(this); }
    static matches(block) { return block.type === "input_text" || block.type === "output_text"; }
    static content(block) { return [text(block.text)]; }
}
class ResponseImage extends ResponseBlock {
    static { ResponseBlock.declarations.push(this); }
    static matches(block) { return block.type === "input_image"; }
    static content() { return [image()]; }
}
function responseContent(content) {
    return stringContent(content) ?? content.flatMap(block => ResponseBlock.decode(block));
}
class ResponseItem extends WireBlock { static declarations = []; }
class ResponseMessage extends ResponseItem {
    static { ResponseItem.declarations.push(this); }
    static matches(row) { return row.type === "message" || (row.type === undefined && row.role !== undefined); }
    static content(row) { return responseContent(row.content); }
}
class ResponseFunctionCall extends ResponseItem {
    static { ResponseItem.declarations.push(this); }
    static matches(row) { return row.type === "function_call"; }
    static content(row) { return [text(row.name + row.arguments)]; }
}
class ResponseFunctionOutput extends ResponseItem {
    static { ResponseItem.declarations.push(this); }
    static matches(row) { return row.type === "function_call_output"; }
    static content(row) { return responseContent(row.output); }
}
class ResponseCustomCall extends ResponseItem {
    static { ResponseItem.declarations.push(this); }
    static matches(row) { return row.type === "custom_tool_call"; }
    static content(row) { return [text(row.name + row.input)]; }
}
class ResponseCustomOutput extends ResponseItem {
    static { ResponseItem.declarations.push(this); }
    static matches(row) { return row.type === "custom_tool_call_output"; }
    static content(row) { return responseContent(row.output); }
}
class ResponseReasoning extends ResponseItem {
    static { ResponseItem.declarations.push(this); }
    static matches(row) { return row.type === "reasoning"; }
    static content(row) { return (row.summary ?? []).map(summary => text(summary.text)); }
}
export function responsesInput(params) {
    const input = stringContent(params.input) ?? params.input.flatMap(row => ResponseItem.decode(row));
    return { systemPrompt: params.instructions, tools: params.tools, messages: [message(input)] };
}

class GooglePart extends WireBlock { static declarations = []; }
class GoogleText extends GooglePart {
    static { GooglePart.declarations.push(this); }
    static matches(part) { return part.text !== undefined; }
    static content(part) { return [text(part.text)]; }
}
class GoogleImage extends GooglePart {
    static { GooglePart.declarations.push(this); }
    static matches(part) { return part.inlineData !== undefined || part.fileData !== undefined; }
    static content() { return [image()]; }
}
class GoogleCall extends GooglePart {
    static { GooglePart.declarations.push(this); }
    static matches(part) { return part.functionCall !== undefined; }
    static content(part) { return [text(JSON.stringify(part.functionCall))]; }
}
class GoogleResult extends GooglePart {
    static { GooglePart.declarations.push(this); }
    static matches(part) { return part.functionResponse !== undefined; }
    static content(part) { return [text(JSON.stringify(part.functionResponse))]; }
}
export function googleInput(params) {
    const system = params.config?.systemInstruction;
    const systemParts = typeof system === "string" ? [text(system)] : (system?.parts ?? []).flatMap(part => GooglePart.decode(part));
    return { tools: params.config?.tools, messages: [message(systemParts),
        ...params.contents.map(row => message(row.parts.flatMap(part => GooglePart.decode(part))))] };
}

class BedrockBlock extends WireBlock { static declarations = []; }
class BedrockText extends BedrockBlock {
    static { BedrockBlock.declarations.push(this); }
    static matches(block) { return block.text !== undefined; }
    static content(block) { return [text(block.text)]; }
}
class BedrockImage extends BedrockBlock {
    static { BedrockBlock.declarations.push(this); }
    static matches(block) { return block.image !== undefined; }
    static content() { return [image()]; }
}
class BedrockCall extends BedrockBlock {
    static { BedrockBlock.declarations.push(this); }
    static matches(block) { return block.toolUse !== undefined; }
    static content(block) { return [text(JSON.stringify(block.toolUse))]; }
}
class BedrockResult extends BedrockBlock {
    static { BedrockBlock.declarations.push(this); }
    static matches(block) { return block.toolResult !== undefined; }
    static content(block) { return block.toolResult.content.flatMap(item => BedrockBlock.decode(item)); }
}
class BedrockJson extends BedrockBlock {
    static { BedrockBlock.declarations.push(this); }
    static matches(block) { return block.json !== undefined; }
    static content(block) { return [text(JSON.stringify(block.json))]; }
}
class BedrockReasoning extends BedrockBlock {
    static { BedrockBlock.declarations.push(this); }
    static matches(block) { return block.reasoningContent !== undefined; }
    static content(block) { return block.reasoningContent.reasoningText ? [text(block.reasoningContent.reasoningText.text)] : []; }
}
export function bedrockInput(params) {
    return { tools: params.toolConfig?.tools, messages: [
        message((params.system ?? []).flatMap(block => BedrockBlock.decode(block))),
        ...params.messages.map(row => message(row.content.flatMap(block => BedrockBlock.decode(block)))),
    ] };
}
