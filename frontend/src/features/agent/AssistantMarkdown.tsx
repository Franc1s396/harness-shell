import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export type AssistantMarkdownProps = {
  text: string;
};

/** Renders untrusted model text as Markdown without enabling raw HTML. */
export function AssistantMarkdown({ text }: AssistantMarkdownProps) {
  return (
    <div className="agent-markdown min-w-0 max-w-full break-words">
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node: _node, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer noopener" />
          ),
          img: ({ node: _node, alt }) => <span>{alt ?? ""}</span>,
          table: ({ node: _node, ...props }) => (
            <div className="agent-markdown-table">
              <table {...props} />
            </div>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
