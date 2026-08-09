#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include "plugin.h"

#include "bridge.h"
#include "live_context.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cctype>
#include <cstdio>
#include <iterator>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace
{
using namespace std::chrono_literals;

constexpr std::size_t MaxMessages = 100;
constexpr std::chrono::seconds RequestTimeout = 210s;
constexpr std::size_t InputCapacity = 2048;
constexpr const char* SourceBlockSeparator = "\n\n__RC_SOURCES_V1__\n";
constexpr const char* SourcePillsCapability = "source-pills-v1";
constexpr const char* SourcePillsContextPrefix = "Companion capabilities: ";
constexpr const char* LiveContextMarker = "__RC_LIVE_CONTEXT_V1__";
constexpr std::size_t MaxSources = 3;

#ifndef MODLOADER_BUILD_TAG
#define MODLOADER_BUILD_TAG "dev"
#endif

struct Message
{
    enum class Author
    {
        Player,
        Companion,
        Error,
    };

    Author author;
    std::string text;
    std::string sourcesHeading;
    std::vector<std::string> sources;
};

struct ChatState
{
    std::mutex mutex;
    std::vector<Message> messages;
    std::string sessionId;
    std::string status = "Ready";
    std::string lastQuestion;
    std::uint64_t sequence = 0;
    std::chrono::steady_clock::time_point waitingSince{};
    bool waiting = false;
    bool canRetry = false;
    bool unread = false;
    bool scrollToBottom = false;
};

IPluginSelf* g_self = nullptr;
PanelHandle g_panel = nullptr;
std::atomic_bool g_panelOpen = false;
std::jthread g_pollThread;
ChatState g_chat;
char g_input[InputCapacity]{};
bool g_enterWasDown = false;
bool g_resetInputWidget = false;
bool g_confirmNewChat = false;
char g_openKey[64] = "F10";

PluginInfo g_pluginInfo = {
    "RuptureCompanion",
    MODLOADER_BUILD_TAG,
    "Lucenx9",
    "An in-game AI companion for StarRupture",
    PLUGIN_INTERFACE_VERSION,
    PLUGIN_TARGET_CLIENT,
};

const ConfigEntry ConfigEntries[] = {
    {"General",
     "Enabled",
     ConfigValueType::Boolean,
     "true",
     "Enable or disable Rupture Companion",
     0.0f,
     0.0f},
    {"General",
     "OpenKey",
     ConfigValueType::Keybind,
     "F10",
     "Open or close the companion chat",
     0.0f,
     0.0f},
};

const ConfigSchema ConfigSchemaDefinition = {
    ConfigEntries,
    static_cast<int>(std::size(ConfigEntries)),
};

void LogInfo(const char* message)
{
    if (g_self != nullptr)
    {
        g_self->logger->Info(g_self, "%s", message);
    }
}

void LogError(const char* message)
{
    if (g_self != nullptr)
    {
        g_self->logger->Error(g_self, "%s", message);
    }
}

std::string Trim(std::string value)
{
    const auto notSpace = [](const unsigned char character) {
        return std::isspace(character) == 0;
    };
    const auto begin = std::find_if(value.begin(), value.end(), notSpace);
    const auto end = std::find_if(value.rbegin(), value.rend(), notSpace).base();
    return begin < end ? std::string(begin, end) : std::string{};
}

void AddMessageLocked(const Message::Author author, std::string text)
{
    Message message{author, std::move(text), {}, {}};
    const std::size_t marker = message.text.rfind(SourceBlockSeparator);
    if (author == Message::Author::Companion && marker != std::string::npos)
    {
        std::istringstream sourceBlock(
            message.text.substr(marker + std::char_traits<char>::length(SourceBlockSeparator)));
        std::string heading;
        std::vector<std::string> sources;
        if (std::getline(sourceBlock, heading) && !heading.empty())
        {
            std::string source;
            while (sources.size() < MaxSources && std::getline(sourceBlock, source))
            {
                if (!source.empty())
                {
                    sources.push_back(std::move(source));
                }
            }
        }
        if (!sources.empty())
        {
            message.text.erase(marker);
            message.sourcesHeading = std::move(heading);
            message.sources = std::move(sources);
        }
    }
    g_chat.messages.push_back(std::move(message));
    while (g_chat.messages.size() > MaxMessages)
    {
        g_chat.messages.erase(g_chat.messages.begin());
    }
    g_chat.scrollToBottom = true;
}

std::string NewSessionId()
{
    const auto milliseconds = std::chrono::duration_cast<std::chrono::milliseconds>(
                                  std::chrono::system_clock::now().time_since_epoch())
                                  .count();
    return std::to_string(milliseconds) + "-" + std::to_string(GetCurrentProcessId());
}

void ResetConversation()
{
    std::uint64_t canceledSequence = 0;
    std::string canceledSession;
    {
        std::scoped_lock lock(g_chat.mutex);
        if (g_chat.waiting)
        {
            canceledSequence = g_chat.sequence;
            canceledSession = g_chat.sessionId;
        }
        ++g_chat.sequence;
        g_chat.sessionId = NewSessionId();
        g_chat.messages.clear();
        g_chat.status = "Ready";
        g_chat.lastQuestion.clear();
        g_chat.waiting = false;
        g_chat.canRetry = false;
        g_chat.unread = false;
        AddMessageLocked(
            Message::Author::Companion,
            "Ask about what is on screen, your next production step, a recipe, or a "
            "current patch. Use /screen for visual details and /web off to stay offline.");
    }
    if (canceledSequence != 0)
    {
        std::string ignoredError;
        RuptureCompanion::Bridge::WriteCancellation(
            canceledSequence, canceledSession, ignoredError);
    }
    g_input[0] = '\0';
    g_resetInputWidget = true;
}

std::string SessionContext()
{
    const char* mode = "Unknown";
    if (g_self != nullptr && g_self->hooks != nullptr && g_self->hooks->NetMode != nullptr)
    {
        switch (g_self->hooks->NetMode->GetNetMode())
        {
        case EPluginNetMode::Standalone:
            mode = "Standalone";
            break;
        case EPluginNetMode::DedicatedServer:
            mode = "Dedicated server";
            break;
        case EPluginNetMode::ListenServer:
            mode = "Listen server";
            break;
        case EPluginNetMode::Client:
            mode = "Multiplayer client";
            break;
        default:
            break;
        }
    }
    std::string context = std::string("Session mode: ") + mode + "\n"
        + SourcePillsContextPrefix + SourcePillsCapability;
    const std::string liveContext = RuptureCompanion::LiveContext::Snapshot();
    if (!liveContext.empty())
    {
        context += "\n";
        context += LiveContextMarker;
        context += "\n";
        context += liveContext;
    }
    return context;
}

bool SubmitQuestion(const std::string& rawQuestion)
{
    const std::string question = Trim(rawQuestion);
    if (question.empty())
    {
        return false;
    }

    std::uint64_t sequence = 0;
    std::string sessionId;
    {
        std::scoped_lock lock(g_chat.mutex);
        if (g_chat.waiting)
        {
            return false;
        }
        sequence = ++g_chat.sequence;
        sessionId = g_chat.sessionId;
        g_chat.waiting = true;
        g_chat.waitingSince = std::chrono::steady_clock::now();
        g_chat.lastQuestion = question;
        g_chat.canRetry = false;
        g_chat.status = "Analyzing game context...";
        AddMessageLocked(Message::Author::Player, question);
    }

    std::string error;
    if (!RuptureCompanion::Bridge::WriteRequest(
            sequence, sessionId, question, SessionContext(), error))
    {
        std::scoped_lock lock(g_chat.mutex);
        if (g_chat.sequence == sequence)
        {
            g_chat.waiting = false;
            g_chat.canRetry = true;
            g_chat.status = error;
            AddMessageLocked(Message::Author::Error, error);
        }
        return false;
    }
    return true;
}

void PollLoop(const std::stop_token stopToken)
{
    while (!stopToken.stop_requested())
    {
        std::uint64_t expectedSequence = 0;
        std::string expectedSession;
        std::chrono::steady_clock::time_point waitingSince;
        {
            std::scoped_lock lock(g_chat.mutex);
            if (g_chat.waiting)
            {
                expectedSequence = g_chat.sequence;
                expectedSession = g_chat.sessionId;
                waitingSince = g_chat.waitingSince;
            }
        }

        if (expectedSequence != 0)
        {
            try
            {
                const auto response = RuptureCompanion::Bridge::ReadResponse();
                if (response.has_value() && response->sequence == expectedSequence
                    && response->sessionId == expectedSession)
                {
                    std::scoped_lock lock(g_chat.mutex);
                    if (g_chat.waiting && g_chat.sequence == expectedSequence)
                    {
                        g_chat.waiting = false;
                        g_chat.canRetry = response->isError;
                        g_chat.status = response->isError ? "Request failed" : "Ready";
                        AddMessageLocked(
                            response->isError ? Message::Author::Error
                                              : Message::Author::Companion,
                            response->text);
                        g_chat.unread = !g_panelOpen.load();
                    }
                }
                else if (std::chrono::steady_clock::now() - waitingSince > RequestTimeout)
                {
                    std::scoped_lock lock(g_chat.mutex);
                    if (g_chat.waiting && g_chat.sequence == expectedSequence)
                    {
                        g_chat.waiting = false;
                        g_chat.canRetry = true;
                        g_chat.status = "Request timed out";
                        AddMessageLocked(
                            Message::Author::Error,
                            "No response arrived within 210 seconds. You can retry.");
                    }
                }
            }
            catch (...)
            {
                LogError("Bridge polling failed unexpectedly");
            }
        }
        std::this_thread::sleep_for(200ms);
    }
}

void TogglePanel(EModKey, EModKeyEvent)
{
    if (g_self == nullptr || g_self->hooks == nullptr || g_self->hooks->UI == nullptr
        || g_panel == nullptr)
    {
        return;
    }
    const bool shouldOpen = !g_panelOpen.load();
    g_panelOpen.store(shouldOpen);
    if (shouldOpen)
    {
        g_self->hooks->UI->SetPanelOpen(g_panel);
    }
    else
    {
        g_self->hooks->UI->SetPanelClose(g_panel);
    }
}

void OnPanelClosed(const PanelHandle handle)
{
    if (handle == g_panel)
    {
        g_panelOpen.store(false);
    }
}

void CancelWaiting()
{
    std::uint64_t sequence = 0;
    std::string sessionId;
    {
        std::scoped_lock lock(g_chat.mutex);
        if (!g_chat.waiting)
        {
            return;
        }
        sequence = g_chat.sequence;
        sessionId = g_chat.sessionId;
        ++g_chat.sequence;
        g_chat.waiting = false;
        g_chat.canRetry = true;
        g_chat.status = "Request canceled";
    }
    std::string error;
    if (!RuptureCompanion::Bridge::WriteCancellation(sequence, sessionId, error))
    {
        LogError(error.c_str());
    }
}

void RenderPanel(IModLoaderImGui* ui)
{
    try
    {
        std::vector<Message> messages;
        std::string status;
        std::string retryQuestion;
        bool waiting = false;
        bool canRetry = false;
        bool unread = false;
        bool scrollToBottom = false;
        std::chrono::steady_clock::time_point waitingSince;
        {
            std::scoped_lock lock(g_chat.mutex);
            messages = g_chat.messages;
            status = g_chat.status;
            retryQuestion = g_chat.lastQuestion;
            waiting = g_chat.waiting;
            canRetry = g_chat.canRetry;
            unread = g_chat.unread;
            scrollToBottom = g_chat.scrollToBottom;
            waitingSince = g_chat.waitingSince;
            g_chat.unread = false;
            g_chat.scrollToBottom = false;
        }

        if (unread)
        {
            ui->TextColored(0.45f, 0.85f, 1.0f, 1.0f, "New reply received");
        }
        if (waiting)
        {
            const auto seconds = std::chrono::duration_cast<std::chrono::seconds>(
                                     std::chrono::steady_clock::now() - waitingSince)
                                     .count();
            status = "Analyzing game context... " + std::to_string(seconds) + "s";
        }
        ui->TextDisabled(status.c_str());
        ui->Separator();

        if (ui->BeginChild("##Transcript", 0.0f, -82.0f, true))
        {
            for (std::size_t messageIndex = 0; messageIndex < messages.size(); ++messageIndex)
            {
                const Message& message = messages[messageIndex];
                switch (message.author)
                {
                case Message::Author::Player:
                    ui->TextColored(0.42f, 0.76f, 1.0f, 1.0f, "You");
                    break;
                case Message::Author::Companion:
                    ui->TextColored(0.38f, 0.92f, 0.70f, 1.0f, "Companion");
                    break;
                case Message::Author::Error:
                    ui->TextColored(1.0f, 0.45f, 0.42f, 1.0f, "Error");
                    break;
                }
                ui->TextWrapped(message.text.c_str());
                if (!message.sources.empty())
                {
                    ui->TextDisabled(message.sourcesHeading.c_str());
                    ui->BeginDisabled(true);
                    for (std::size_t sourceIndex = 0; sourceIndex < message.sources.size();
                         ++sourceIndex)
                    {
                        if (sourceIndex > 0)
                        {
                            ui->SameLine(0.0f, 6.0f);
                        }
                        ui->PushIDInt(static_cast<int>(messageIndex));
                        ui->PushIDInt(static_cast<int>(sourceIndex));
                        ui->SmallButton(message.sources[sourceIndex].c_str());
                        ui->PopID();
                        ui->PopID();
                    }
                    ui->EndDisabled();
                }
                ui->Spacing();
            }
            if (scrollToBottom)
            {
                ui->SetScrollHereY(1.0f);
            }
        }
        ui->EndChild();

        const bool resetInputWidget = g_resetInputWidget;
        g_resetInputWidget = false;
        ui->SetNextItemWidth(-92.0f);
        if (resetInputWidget)
        {
            // InputText keeps an internal edit buffer while its ImGui ID is active.
            // Skip that ID for one frame after clearing our buffer so the stale edit
            // state cannot copy the submitted question back on the following frame.
            ui->BeginDisabled(true);
            ui->InputTextWithHint(
                "##QuestionReset", "Ask Rupture Companion...", g_input, std::size(g_input));
            ui->EndDisabled();
        }
        else
        {
            ui->InputTextWithHint(
                "##Question", "Ask Rupture Companion...", g_input, std::size(g_input));
        }
        // Single-line InputText clears its active ID before returning on Enter,
        // but it keeps keyboard focus. IsItemFocused therefore preserves the
        // submission edge that IsItemActive would miss in that frame.
        const bool inputFocused = !resetInputWidget && ui->IsItemFocused();
        const bool enterDown = (GetAsyncKeyState(VK_RETURN) & 0x8000) != 0;
        const bool submitWithEnter = inputFocused && enterDown && !g_enterWasDown;
        g_enterWasDown = enterDown;
        ui->SameLine(0.0f, 6.0f);
        ui->BeginDisabled(waiting || Trim(g_input).empty());
        const bool submitWithButton = ui->ButtonSized("Send", 82.0f, 0.0f);
        ui->EndDisabled();

        if ((submitWithButton || submitWithEnter) && SubmitQuestion(g_input))
        {
            g_input[0] = '\0';
            g_resetInputWidget = true;
            g_confirmNewChat = false;
        }

        if (waiting)
        {
            if (ui->SmallButton("Cancel request"))
            {
                CancelWaiting();
            }
            ui->SameLine(0.0f, 8.0f);
        }
        else if (canRetry && !retryQuestion.empty())
        {
            if (ui->SmallButton("Retry"))
            {
                SubmitQuestion(retryQuestion);
            }
            ui->SameLine(0.0f, 8.0f);
        }

        const char* newChatLabel = g_confirmNewChat ? "Confirm new chat" : "New chat";
        if (ui->SmallButton(newChatLabel))
        {
            if (g_confirmNewChat)
            {
                ResetConversation();
                g_confirmNewChat = false;
            }
            else
            {
                g_confirmNewChat = true;
            }
        }
    }
    catch (...)
    {
        LogError("Chat rendering failed unexpectedly");
    }
}
} // namespace

extern "C"
{
__declspec(dllexport) PluginInfo* GetPluginInfo()
{
    return &g_pluginInfo;
}

__declspec(dllexport) bool PluginInit(IPluginSelf* self)
{
    try
    {
        g_self = self;
        if (self == nullptr || self->hooks == nullptr || self->config == nullptr)
        {
            return false;
        }
        self->config->InitializeFromSchema(self, &ConfigSchemaDefinition);
        if (!self->config->ReadBool(self, "General", "Enabled", true))
        {
            LogInfo("Plugin is disabled in config");
            return true;
        }
        if (self->hooks->UI == nullptr || self->hooks->Input == nullptr)
        {
            LogError("The client UI and input interfaces are unavailable");
            return false;
        }
        self->config->ReadString(
            self, "General", "OpenKey", g_openKey, static_cast<int>(std::size(g_openKey)), "F10");

        static const PluginPanelDesc panelDescription = {
            "Rupture Companion",
            "Rupture Companion",
            &RenderPanel,
        };
        g_panel = self->hooks->UI->RegisterPanel(&panelDescription);
        if (g_panel == nullptr)
        {
            LogError("Could not register the chat panel");
            return false;
        }
        self->hooks->UI->RegisterOnPanelWindowClosed(&OnPanelClosed);
        self->hooks->Input->RegisterKeybindByName(
            g_openKey, EModKeyEvent::Pressed, &TogglePanel);
        ResetConversation();
        g_pollThread = std::jthread(&PollLoop);
        LogInfo("Plugin initialized; press the configured key to open the chat");
        // Keep live hook registration as the final potentially successful init
        // step. If constructing the worker thread throws, no game-thread cleanup
        // barrier has been installed yet.
        if (!RuptureCompanion::LiveContext::Initialize(self))
        {
            LogError("Live game-state hooks are unavailable; screenshot fallback remains active");
        }
        return true;
    }
    catch (...)
    {
        LogError("Plugin initialization failed unexpectedly");
        PluginShutdown();
        return false;
    }
}

__declspec(dllexport) void PluginShutdown()
{
    try
    {
        RuptureCompanion::LiveContext::Shutdown();
        if (g_pollThread.joinable())
        {
            g_pollThread.request_stop();
            g_pollThread.join();
        }
        if (g_self != nullptr && g_self->hooks != nullptr)
        {
            if (g_self->hooks->Input != nullptr)
            {
                g_self->hooks->Input->UnregisterKeybindByName(
                    g_openKey, EModKeyEvent::Pressed, &TogglePanel);
            }
            if (g_self->hooks->UI != nullptr)
            {
                g_self->hooks->UI->UnregisterOnPanelWindowClosed(&OnPanelClosed);
                if (g_panel != nullptr)
                {
                    g_self->hooks->UI->UnregisterPanel(g_panel);
                }
            }
        }
    }
    catch (...)
    {
    }
    g_panel = nullptr;
    g_panelOpen.store(false);
    g_self = nullptr;
}
}
