import axios from "axios";

async function customSendMessage(request, _options, instance) {
  const userInput = request.input.text;

  if (userInput === '') {
    if (instance.messaging && instance.messaging.addMessage && typeof instance.messaging.addMessage === 'function') {
      // sendWelcomeMessage(instance);
      return;
    }
  }

  try {
 
    const response = await axios.post('/generate',  {
      prompt: userInput , 
    });

    // const replyText = response.data?.choices?.[0]?.message?.content;
    const replyText = response.data?.response || " ";
    const docs = response.data?.documents || [];
    // const docs = [{"name": "Doc 1", "link": "https://www.ibm.com/docs/en/power10/9105-22A?topic=overview-hmc-operations"},
    //    {"name": "Doc 2", "link": "https://www.ibm.com/docs/en/power10/9105-22A?topic=overview-hmc-operations"}]

    // Main text response
    instance.messaging.addMessage({
      output: {
        generic: [
          {
            response_type: "text",
            text: replyText || "No response received.",
          },
          {
            response_type: "user_defined",
            user_defined: {
              user_defined_type: "reference_docs_button",
              docs,
              original_text: replyText,
              button_label: "Get reference documents",
            },
          },
        ],
      },
    });
  } catch (err) {
    console.error("Error contacting RAG server:", err);
    instance.messaging.addMessage({
      output: {
        generic: [
          {
            response_type: "text",
            text: "Sorry, something went wrong contacting the server.",
          },
        ],
      },
    });
  }
}

export { customSendMessage };
