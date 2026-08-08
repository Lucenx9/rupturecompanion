#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include "bridge.h"

#include <charconv>
#include <fstream>
#include <sstream>
#include <system_error>
#include <vector>

namespace RuptureCompanion::Bridge
{
namespace
{
constexpr const char* EndMarker = "__RC_END__";
constexpr std::uintmax_t MaxResponseBytes = 256 * 1024;

std::string SanitizeLine(std::string value)
{
    for (char& character : value)
    {
        if (character == '\r' || character == '\n')
        {
            character = ' ';
        }
    }
    return value;
}

std::string SanitizeContext(const std::string& value)
{
    std::istringstream input(value);
    std::ostringstream output;
    std::string line;
    bool first = true;
    while (std::getline(input, line))
    {
        if (!line.empty() && line.back() == '\r')
        {
            line.pop_back();
        }
        if (!first)
        {
            output << '\n';
        }
        output << (line == EndMarker ? "[__RC_END__]" : line);
        first = false;
    }
    return output.str();
}

bool AtomicReplace(
    const std::filesystem::path& temporary,
    const std::filesystem::path& destination)
{
    return MoveFileExW(
               temporary.c_str(),
               destination.c_str(),
               MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)
        != FALSE;
}

void StripCarriageReturn(std::string& line)
{
    if (!line.empty() && line.back() == '\r')
    {
        line.pop_back();
    }
}
} // namespace

std::filesystem::path Directory()
{
    std::vector<wchar_t> buffer(32768);
    const DWORD length = GetModuleFileNameW(
        nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    if (length == 0 || length >= buffer.size())
    {
        return std::filesystem::current_path() / L"RuptureCompanion";
    }
    return std::filesystem::path(buffer.data(), buffer.data() + length).parent_path()
        / L"RuptureCompanion";
}

bool WriteRequest(
    const std::uint64_t sequence,
    const std::string& sessionId,
    const std::string& question,
    const std::string& context,
    std::string& error)
{
    const auto directory = Directory();
    const auto temporary = directory / L"question.tmp";
    const auto destination = directory / L"question.txt";
    std::error_code filesystemError;
    std::filesystem::create_directories(directory, filesystemError);
    if (filesystemError)
    {
        error = "Could not create the bridge directory";
        return false;
    }

    {
        std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
        if (!output)
        {
            error = "Could not open the request file";
            return false;
        }
        output << "v1|" << sequence << '|' << SanitizeLine(sessionId) << '|'
               << SanitizeLine(question) << '\n';
        const std::string safeContext = SanitizeContext(context);
        if (!safeContext.empty())
        {
            output << safeContext << '\n';
        }
        output << EndMarker << '\n';
        output.flush();
        if (!output)
        {
            error = "Could not write the request file";
            return false;
        }
    }

    if (!AtomicReplace(temporary, destination))
    {
        std::filesystem::remove(temporary, filesystemError);
        error = "Could not publish the request file";
        return false;
    }
    return true;
}

std::optional<Response> ReadResponse()
{
    const auto path = Directory() / L"answer.txt";
    std::error_code error;
    const std::uintmax_t size = std::filesystem::file_size(path, error);
    if (error || size == 0 || size > MaxResponseBytes)
    {
        return std::nullopt;
    }

    std::ifstream input(path, std::ios::binary);
    if (!input)
    {
        return std::nullopt;
    }
    std::string header;
    if (!std::getline(input, header))
    {
        return std::nullopt;
    }
    StripCarriageReturn(header);
    const auto separator = header.find('|');
    if (separator == std::string::npos)
    {
        return std::nullopt;
    }
    std::uint64_t sequence = 0;
    const auto parse = std::from_chars(
        header.data(), header.data() + separator, sequence);
    if (parse.ec != std::errc{} || parse.ptr != header.data() + separator)
    {
        return std::nullopt;
    }
    const std::string status = header.substr(separator + 1);
    if (status != "ok" && status != "error")
    {
        return std::nullopt;
    }

    std::vector<std::string> lines;
    std::string line;
    while (std::getline(input, line))
    {
        StripCarriageReturn(line);
        lines.push_back(line);
    }
    if (lines.empty() || lines.back() != EndMarker)
    {
        return std::nullopt;
    }
    lines.pop_back();

    std::ostringstream text;
    for (std::size_t index = 0; index < lines.size(); ++index)
    {
        if (index > 0)
        {
            text << '\n';
        }
        text << lines[index];
    }
    if (text.str().empty())
    {
        return std::nullopt;
    }
    return Response{sequence, status == "error", text.str()};
}
} // namespace RuptureCompanion::Bridge
