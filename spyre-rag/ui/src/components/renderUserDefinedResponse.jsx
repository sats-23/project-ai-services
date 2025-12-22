import { ReferenceDocsButton } from './ReferenceDocsButton.jsx';
import FeedbackWidget from './FeedbackWidget.jsx';

function renderUserDefinedResponse(state) {
  const { messageItem } = state;
  const type = messageItem?.user_defined?.user_defined_type;

  switch (type) {
    case 'reference_docs_button':
      return <ReferenceDocsButton data={messageItem.user_defined} />;

    case 'feedback_hub_widget':
      return (
        <FeedbackWidget 
          userInput={messageItem.user_defined.userInput}
          aiResponse={messageItem.user_defined.aiResponse}
          projectId={messageItem.user_defined.projectId}
        />
      );

    default:
      return undefined;
  }
}

export { renderUserDefinedResponse };
