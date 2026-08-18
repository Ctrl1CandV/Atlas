import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';

function isExternalLink(href: string): boolean {
  return /^(?:https?:)?\/\//i.test(href) || /^mailto:/i.test(href);
}

/**
 * Shared safe Markdown renderer. Raw HTML is never parsed, links keep the
 * react-markdown protocol allow-list, and external targets cannot control the opener.
 */
export function SafeMarkdown({
  children,
  onNavigate,
}: {
  children: string;
  onNavigate?: (hash: string) => void;
}) {
  return (
    <ReactMarkdown
      skipHtml
      remarkPlugins={[remarkGfm]}
      urlTransform={(url) => defaultUrlTransform(url)}
      components={{
        a: ({ href, children: linkChildren }) => {
          if (href?.startsWith('#/') && onNavigate) {
            return (
              <a
                href={href}
                onClick={(event) => {
                  event.preventDefault();
                  onNavigate(href);
                }}
              >
                {linkChildren}
              </a>
            );
          }
          if (href && isExternalLink(href)) {
            return <a href={href} target="_blank" rel="noopener noreferrer">{linkChildren}</a>;
          }
          return <a href={href}>{linkChildren}</a>;
        },
      }}
    >
      {children}
    </ReactMarkdown>
  );
}
